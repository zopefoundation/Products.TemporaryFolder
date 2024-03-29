##############################################################################
#
# Copyright (c) 2001, 2002 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE
#
##############################################################################
"""Mounted database support
"""

import logging
import threading
import time

import persistent
from Acquisition import Implicit
from Acquisition import ImplicitAcquisitionWrapper
from Acquisition import aq_base
from ZODB.POSException import StorageError


logger = logging.getLogger('ZODB.Mount')

# dbs is a holder for all DB objects, needed to overcome
# threading issues.  It maps connection params to a DB object
# and a mapping of mount points.
dbs = {}

# dblock is locked every time dbs is accessed.
dblock = threading._allocate_lock()


class MountedStorageError(StorageError):
    """Unable to access mounted storage."""


def parentClassFactory(jar, module, name):
    # Use the class factory from the parent database.
    parent_conn = getattr(jar, '_mount_parent_jar', None)
    parent_db = getattr(parent_conn, '_db', None)
    if parent_db is None:
        _globals = {}
        _silly = ('__doc__',)
        return getattr(__import__(
            module, _globals, _globals, _silly), name)
    else:
        return parent_db.classFactory(parent_conn, module, name)


class MountPoint(persistent.Persistent, Implicit):
    """The base class for a Zope object which, when traversed,
    accesses a different database.
    """

    # Default values for non-persistent variables.
    _v_db = None
    _v_data = None
    _v_connect_error = None

    def __init__(self, path, params=None, classDefsFromRoot=None):
        """
        @arg path The path within the mounted database from which
        to derive the root.

        @arg params The parameters used to connect to the database.
        No particular format required.
        If there is more than one mount point referring to a
        database, MountPoint will detect the matching params
        and use the existing database.  Include the class name of
        the storage.  For example,
        ZEO params might be "ZODB.ZEOClient localhost 1081".
        """
        # The only reason we need a __mountpoint_id is to
        # be sure we don't close a database prematurely when
        # it is mounted more than once and one of the points
        # is unmounted.
        self.__mountpoint_id = f'{id(self)}_{time.time():f}'
        if params is None:
            # We still need something to use as a hash in
            # the "dbs" dictionary.
            params = self.__mountpoint_id
        self._params = repr(params)
        self._path = path

    def _createDB(self):
        """Gets the database object, usually by creating a Storage object
        and returning ZODB.DB(storage).
        """
        raise NotImplementedError

    def _getDB(self):
        """Creates or opens a DB object.
        """
        newMount = 0
        with dblock:
            params = self._params
            dbInfo = dbs.get(params, None)
            if dbInfo is None:
                logger.info('Opening database for mounting: %s', params)
                db = self._createDB()
                newMount = 1
                dbs[params] = (db, {self.__mountpoint_id: 1})
            else:
                db, mounts = dbInfo
                # Be sure this object is in the list of mount points.
                if self.__mountpoint_id not in mounts:
                    newMount = 1
                    mounts[self.__mountpoint_id] = 1
            self._v_db = db
        return db, newMount

    def _getMountpointId(self):
        return self.__mountpoint_id

    def _getMountParams(self):
        return self._params

    def __repr__(self):
        return f'{self.__class__.__name__}({self._path!r}, {self._params})'

    def _openMountableConnection(self, parent):
        # Opens a new connection to the database.
        db = self._v_db
        if db is None:
            self._v_close_db = 0
            db, newMount = self._getDB()
        else:
            newMount = 0
        jar = getattr(self, '_p_jar', None)
        if jar is None:
            # Get _p_jar from parent.
            self._p_jar = jar = parent._p_jar
        conn = db.open()

        # Add an attribute to the connection which
        # makes it possible for us to find the primary
        # database connection.  See ClassFactoryForMount().
        conn._mount_parent_jar = jar

        mcc = MountedConnectionCloser(self, conn)
        jar.onCloseCallback(mcc)
        return conn, newMount, mcc

    def _getObjectFromConnection(self, conn):
        obj = self._getMountRoot(conn.root())
        data = aq_base(obj)
        # Store the data object in a tuple to hide from acquisition.
        self._v_data = (data,)
        return data

    def _getOrOpenObject(self, parent):
        t = self._v_data
        if t is None:
            self._v_connect_error = None
            conn = None
            newMount = 0
            mcc = None
            try:
                conn, newMount, mcc = self._openMountableConnection(parent)
                data = self._getObjectFromConnection(conn)
            except Exception:
                # Possibly broken database.
                if mcc is not None:
                    # Note that the next line may be a little rash--
                    # if, for example, a working database throws an
                    # exception rather than wait for a new connection,
                    # this will likely cause the database to be closed
                    # prematurely.  Perhaps DB.py needs a
                    # countActiveConnections() method.
                    mcc.setCloseDb()
                logger.warning('Failed to mount database. %s (%s)',
                               exc_info=True)
                raise
            if newMount:
                try:
                    id = data.getId()
                except Exception:
                    id = '???'  # data has no getId() method.  Bad.
                p = '/'.join(parent.getPhysicalPath() + (id,))
                logger.info('Mounted database %s at %s',
                            self._getMountParams(), p)
        else:
            data = t[0]

        return data.__of__(parent)

    def __of__(self, parent):
        # Accesses the database, returning an acquisition
        # wrapper around the connected object rather than around self.
        try:
            return self._getOrOpenObject(parent)
        except Exception:
            return ImplicitAcquisitionWrapper(self, parent)

    def _test(self, parent):
        """Tests the database connection.
        """
        self._getOrOpenObject(parent)
        return 1

    def _getMountRoot(self, root):
        """Gets the object to be mounted.
        Can be overridden to provide different behavior.
        """
        try:
            app = root['Application']
        except Exception:
            raise MountedStorageError(
                "No 'Application' object exists in the mountable database.")
        try:
            return app.unrestrictedTraverse(self._path)
        except Exception:
            raise MountedStorageError(
                "The path '%s' was not found in the mountable database."
                % self._path)


class MountedConnectionCloser:
    """Closes the connection used by the mounted database
    while performing other cleanup.
    """
    close_db = 0

    def __init__(self, mountpoint, conn):
        # conn is the child connection.
        self.mp = mountpoint
        self.conn = conn

    def setCloseDb(self):
        self.close_db = 1

    def __call__(self):
        # The onCloseCallback handler.
        # Closes a single connection to the database
        # and possibly the database itself.
        conn = self.conn
        close_db = 0
        if conn is not None:
            mp = self.mp
            # Remove potential circular references.
            self.conn = None
            self.mp = None
            # Detect whether we should close the database.
            close_db = self.close_db
            t = mp.__dict__.get('_v_data', None)
            if t is not None:
                del mp.__dict__['_v_data']
                data = t[0]
                if not close_db and data.__dict__.get(
                        '_v__object_deleted__', 0):
                    # This mount point has been deleted.
                    del data.__dict__['_v__object_deleted__']
                    close_db = 1
            # Close the child connection.
            try:
                del conn._mount_parent_jar
            except Exception:
                pass
            conn.close()

        if close_db:
            # Stop using this database. Close it if no other
            # MountPoint is using it.
            with dblock:
                params = mp._getMountParams()
                mp._v_db = None
                if params in dbs:
                    dbInfo = dbs[params]
                    db, mounts = dbInfo
                    try:
                        del mounts[mp._getMountpointId()]
                    except Exception:
                        pass
                    if len(mounts) < 1:
                        # No more mount points are using this database.
                        del dbs[params]
                        db.close()
                        logger.info('Closed database: %s', params)
