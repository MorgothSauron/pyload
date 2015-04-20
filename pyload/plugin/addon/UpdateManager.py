# -*- coding: utf-8 -*-

from __future__ import with_statement

import os
import re
import sys
import time

from operator import itemgetter

from pyload.network.RequestFactory import getURL
from pyload.plugin.Addon import Expose, Addon, threaded
from pyload.utils import fs_join
from pyload import __status_code__ as release_status


# Case-sensitive os.path.exists
def exists(path):
    if os.path.exists(path):
        if os.name == 'nt':
            dir, name = os.path.split(path)
            return name in os.listdir(dir)
        else:
            return True
    else:
        return False


class UpdateManager(Addon):
    __name    = "UpdateManager"
    __type    = "addon"
    __version = "0.51"

    __config  = [("activated", "bool", "Activated", False),
                 ("checkinterval", "int", "Check interval in hours", 8),
                 ("autorestart", "bool",
                  "Auto-restart pyLoad when required", True),
                 ("checkonstart", "bool", "Check for updates on startup", True),
                 ("checkperiod", "bool",
                  "Check for updates periodically", True),
                 ("reloadplugins", "bool",
                  "Monitor plugin code changes in debug mode", True),
                 ("nodebugupdate", "bool", "Don't update plugins in debug mode", False)]

    __description = """ Check for updates """
    __license     = "GPLv3"
    __authors     = [("Walter Purcaro", "vuolter@gmail.com")]

    SERVER_URL         = "http://updatemanager.pyload.org" if release_status == 5 else None
    MIN_CHECK_INTERVAL = 3 * 60 * 60  #: 3 hours

    event_list = ["allDownloadsProcessed"]


    def activate(self):
        if self.checkonstart:
            self.update()

        self.initPeriodical()


    def setup(self):
        self.interval = 10
        self.info     = {'pyload': False, 'version': None, 'plugins': False, 'last_check': time.time()}
        self.mtimes   = {}  #: store modification time for each plugin

        if self.getConfig('checkonstart'):
            self.core.api.pauseServer()
            self.checkonstart = True
        else:
            self.checkonstart = False

        self.do_restart = False


    def allDownloadsProcessed(self):
        if self.do_restart is True:
            self.logWarning(_("Downloads are done, restarting pyLoad to reload the updated plugins"))
            self.core.api.restart()


    def periodical(self):
        if self.core.debug:
            if self.getConfig('reloadplugins'):
                self.autoreloadPlugins()

            if self.getConfig('nodebugupdate'):
                return

        if self.getConfig('checkperiod') \
           and time.time() - max(self.MIN_CHECK_INTERVAL, self.getConfig('checkinterval') * 60 * 60) > self.info['last_check']:
            self.update()


    @Expose
    def autoreloadPlugins(self):
        """ reload and reindex all modified plugins """
        modules = filter(
            lambda m: m and (m.__name__.startswith("pyload.plugin.") or
                             m.__name__.startswith("userplugins.")) and
            m.__name__.count(".") >= 2, sys.modules.itervalues()
        )

        reloads = []

        for m in modules:
            root, type, name = m.__name__.rsplit(".", 2)
            id = (type, name)
            if type in self.core.pluginManager.plugins:
                f = m.__file__.replace(".pyc", ".py")
                if not os.path.isfile(f):
                    continue

                mtime = os.stat(f).st_mtime

                if id not in self.mtimes:
                    self.mtimes[id] = mtime
                elif self.mtimes[id] < mtime:
                    reloads.append(id)
                    self.mtimes[id] = mtime

        return bool(self.core.pluginManager.reloadPlugins(reloads))


    def server_response(self):
        try:
            return getURL(self.SERVER_URL, get={'v': self.core.api.getServerVersion()}).splitlines()

        except Exception:
            self.logWarning(_("Unable to retrieve server to get updates"))


    @Expose
    @threaded
    def update(self):
        """ check for updates """

        self.core.api.pauseServer()

        if self._update() is 2 and self.getConfig('autorestart'):
            if not self.core.api.statusDownloads():
                self.core.api.restart()
            else:
                self.do_restart = True
                self.logWarning(_("Downloads are active, will restart once the download is done"))
        else:
            self.core.api.unpauseServer()


    def _update(self):
        data = self.server_response()

        self.info['last_check'] = time.time()

        if not data:
            exitcode = 0

        elif data[0] == "None":
            self.logInfo(_("No new pyLoad version available"))
            exitcode = self._updatePlugins(data[1:])

        elif onlyplugin:
            exitcode = 0

        else:
            self.logInfo(_("***  New pyLoad Version %s available  ***") % data[0])
            self.logInfo(_("***  Get it here: https://github.com/pyload/pyload/releases  ***"))
            self.info['pyload']  = True
            self.info['version'] = data[0]
            exitcode = 3

        # Exit codes:
        # -1 = No plugin updated, new pyLoad version available
        #  0 = No plugin updated
        #  1 = Plugins updated
        #  2 = Plugins updated, but restart required
        return exitcode


    def _updatePlugins(self, data):
        """ check for plugin updates """

        exitcode = 0
        updated  = []

        url    = data[0]
        schema = data[1].split('|')

        VERSION = re.compile(r'__version.*=.*("|\')([\d.]+)')

        if "BLACKLIST" in data:
            blacklist  = data[data.index('BLACKLIST') + 1:]
            updatelist = data[2:data.index('BLACKLIST')]
        else:
            blacklist  = []
            updatelist = data[2:]

        updatelist = [dict(zip(schema, x.split('|'))) for x in updatelist]
        blacklist  = [dict(zip(schema, x.split('|'))) for x in blacklist]

        if blacklist:
            type_plugins = [(plugin['type'], plugin['name'].rsplit('.', 1)[0]) for plugin in blacklist]

            # Protect UpdateManager from self-removing
            try:
                type_plugins.remove(("addon", "UpdateManager"))
            except ValueError:
                pass

            for t, n in type_plugins:
                for idx, plugin in enumerate(updatelist):
                    if n == plugin['name'] and t == plugin['type']:
                        updatelist.pop(idx)
                        break

            for t, n in self.removePlugins(sorted(type_plugins)):
                self.logInfo(_("Removed blacklisted plugin: [%(type)s] %(name)s") % {
                    'type': t,
                    'name': n,
                })

        for plugin in sorted(updatelist, key=itemgetter("type", "name")):
            filename = plugin['name']
            type     = plugin['type']
            version  = plugin['version']

            if filename.endswith(".pyc"):
                name = filename[:filename.find("_")]
            else:
                name = filename.replace(".py", "")

            plugins = getattr(self.core.pluginManager, "%sPlugins" % type)

            oldver = float(plugins[name]['version']) if name in plugins else None
            newver = float(version)

            if not oldver:
                msg = "New plugin: [%(type)s] %(name)s (v%(newver).2f)"
            elif newver > oldver:
                msg = "New version of plugin: [%(type)s] %(name)s (v%(oldver).2f -> v%(newver).2f)"
            else:
                continue

            self.logInfo(_(msg) % {'type': type,
                                   'name': name,
                                   'oldver': oldver,
                                   'newver': newver})
            try:
                content = getURL(url % plugin)
                m = VERSION.search(content)

                if m and m.group(2) == version:
                    with open(fs_join("userplugins", type, filename), "wb") as f:
                        f.write(content)

                    updated.append((type, name))
                else:
                    raise Exception, _("Version mismatch")

            except Exception, e:
                self.logError(_("Error updating plugin: %s") % filename, e)

        if updated:
            self.logInfo(_("*** Plugins updated ***"))

            if self.core.pluginManager.reloadPlugins(updated):
                exitcode = 1
            else:
                self.logWarning(_("pyLoad restart required to reload the updated plugins"))
                self.info['plugins'] = True
                exitcode = 2

            self.manager.dispatchEvent("plugin_updated", updated)
        else:
            self.logInfo(_("No plugin updates available"))

        # Exit codes:
        # 0 = No plugin updated
        # 1 = Plugins updated
        # 2 = Plugins updated, but restart required
        return exitcode


    @Expose
    def removePlugins(self, type_plugins):
        """ delete plugins from disk """

        if not type_plugins:
            return

        removed = set()

        self.logDebug("Requested deletion of plugins: %s" % type_plugins)

        for type, name in type_plugins:
            rootplugins = os.path.join(pypath, "module", "plugins")

            for dir in ("userplugins", rootplugins):
                py_filename  = fs_join(dir, type, name + ".py")
                pyc_filename = py_filename + "c"

                if type == "addon":
                    try:
                        self.manager.deactivateAddon(name)

                    except Exception, e:
                        self.logDebug(e)

                for filename in (py_filename, pyc_filename):
                    if not exists(filename):
                        continue

                    try:
                        os.remove(filename)

                    except OSError, e:
                        self.logError(_("Error removing: %s") % filename, e)

                    else:
                        id = (type, name)
                        removed.add(id)

        #: return a list of the plugins successfully removed
        return list(removed)
