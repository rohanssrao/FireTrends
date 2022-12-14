#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#    xdelta3-dir-patcher
#    Copyright (C) 2014-2016 Endless Mobile
#
#   This library is free software; you can redistribute it and/or
#   modify it under the terms of the GNU Lesser General Public
#   License as published by the Free Software Foundation; either
#   version 2.1 of the License, or (at your option) any later version.
#
#   This library is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   Lesser General Public License for more details.
#
#   You should have received a copy of the GNU Lesser General Public
#   License along with this library; if not, write to the Free Software
#   Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
#   USA

import argparse
import errno
import logging
import operator
import random
import tarfile
import time
import threading
import zipfile
import sys

import multiprocessing
import concurrent.futures

from collections import OrderedDict
from filecmp import dircmp
from io import StringIO
from multiprocessing import cpu_count
from os import chmod, listdir, lstat, mkdir, name as os_name
from os import path, readlink, remove, rmdir, symlink, sep
from os import stat, utime, walk
from shutil import copymode, copystat, copyfile, copytree, copy2, rmtree
from stat import *
from subprocess import check_output, STDOUT, CalledProcessError
from sys import hexversion, stderr, stdout
from tempfile import mkdtemp

if os_name != "nt":
    from grp import getgrgid
    from os import geteuid, lchown
    from pwd import getpwuid

from os import open as os_open  # Prevent mangling the regular open()
from os import makedirs as os_makedirs

VERSION = "0.6.4"

if hexversion < 0x30401F0:
    # Handle makedirs throwing EEXIST if the leaf directory mode doesn't
    # match on python < 3.4.1
    def makedirs(*args, **kwargs):
        """Wrapper around os.makedirs on python < 3.4.1 to ignore EEXIST when
        exist_ok=True.
        """
        exist_ok = kwargs.get("exist_ok", False)
        try:
            os_makedirs(*args, **kwargs)
        except OSError as err:
            if not exist_ok or err.errno != errno.EEXIST:
                raise

else:
    # Use regular os.makedirs
    makedirs = os_makedirs

# Allows for invoking attributes as methods/functions
class AttributeDict(dict):
    def __getattr__(self, attr):
        return self[attr]

    def __setattr__(self, attr, value):
        self[attr] = value


# ---------------------------- DIR LISTING ----------------------------
class DirListing(object):
    def __init__(self, name=None):
        self._files = []
        self._dirs = []

        self.is_dir = True
        self.is_file = False
        self.is_link = False
        self.data = None

        self.name = name

    def set_metadata(
        self, name, data, permissions, uname, uid, gname, gid, is_link, link_target=None
    ):
        self.name = name
        self.data = data
        self.permissions = permissions
        self.uname = uname
        self.uid = uid
        self.gname = gname
        self.gid = gid
        self.is_link = is_link
        self.link_target = link_target

    @property
    def dirs(self):
        return self._dirs

    @property
    def files(self):
        return self._files

    def add_subdir(self, subdir):
        self._dirs.append(subdir)

    def add_file(
        self, name, data, permissions, uname, uid, gname, gid, is_link, link_target=None
    ):
        file_dict = {
            "name": name,
            "permissions": permissions,
            "data": data,
            "uname": uname,
            "uid": uid,
            "gname": gname,
            "gid": gid,
            "is_link": is_link,
            "is_file": True,
            "is_dir": False,
            "link_target": link_target,
        }

        self._files.append(AttributeDict(file_dict))

        return self._files[-1]

    def _formatted_file_str(self, file_obj):
        output_str = file_obj.name

        permission_letters = "rwxrwxrwx"
        permission_masks = [
            S_IRUSR,
            S_IWUSR,
            S_IXUSR,
            S_IRGRP,
            S_IWGRP,
            S_IXGRP,
            S_IROTH,
            S_IWOTH,
            S_IXOTH,
        ]

        permissions = ""
        if file_obj.permissions:
            permissions += "("
            for letter, mask in zip(permission_letters, permission_masks):
                permissions += letter if mask & file_obj.permissions else "-"
            permissions += ")"

        is_link = "-> %s" % file_obj.link_target if file_obj.is_link else ""

        output_str = "%s %s %s" % (permissions, output_str, is_link)
        return output_str

    def _print_dir_listing(self, root, output, root_path=""):
        assert root and root.name, "Cannot print listing in path: '%s'" % root_path

        relative_path = path.join(root_path, root.name)
        padding = relative_path.count(path.sep)
        is_link = "-> %s" % root.link_target if root.is_link else ""

        print("| " * padding + "v", relative_path, is_link, file=output)

        for subdir in root.dirs:
            dir_path = path.join(root_path, root.name)
            if not root_path:
                dir_path = path.sep

            self._print_dir_listing(subdir, output, dir_path)

        for filename in root.files:
            print("| " * padding + "-", self._formatted_file_str(filename), file=output)

    def __repr__(self):
        return self.__str__()

    def __str__(self):
        output = StringIO()

        print("-" * 70, file=output)
        self._print_dir_listing(self, output)
        print("-" * 70, file=output)

        return output.getvalue()


# ---------------------------- PROCESS RUNNER ----------------------------
class ExecutorRunner(object):
    def __init__(self, debug=False):
        # multiprocessing.log_to_stderr(logging.DEBUG)

        self.futures = []
        self.start_time = None
        thread_count = max(cpu_count() - 1, 1)

        self.debug = debug

        self.executor = concurrent.futures.ThreadPoolExecutor(thread_count)

    def _fix_terminal(self):
        stdout.flush()
        print()

    def add_task(self, target_func, target_func_args):
        if self.start_time == None:
            self.start_time = time.time()

        self.futures.append(self.executor.submit(target_func, *target_func_args))

        # XXX: For single-threaded debugging
        # target_func(*target_func_args)

    def join_all(self):
        if self.debug:
            # Make sure that the terminal isn't in some strange state
            self._fix_terminal()

            print("Waiting for tasks to finish...")

        # Prevent further scheduling
        self.executor.shutdown(False)

        for future in concurrent.futures.as_completed(self.futures):
            if future.exception():
                raise future.exception()

        # Leftover runners might have again clobbered the output
        self._fix_terminal()

        # XXX: If nothing was ran, we might not have a start time
        if not self.start_time:
            self.start_time = time.time()

        print("Runner time: %.2fs" % (time.time() - self.start_time))


# ---------------------------- ARCHIVE ADAPTERS ----------------------------
class XDeltaArchive(object):
    def __init__(self, archive_path):
        self.archive_object = XDeltaArchive.get_archive_instance(archive_path)

    def __enter__(self):
        return self.archive_object

    def __exit__(self, exc_type, exc_value, traceback):
        self.archive_object.close()

    @staticmethod
    def get_archive_instance(archive_path):
        for clazz in XDelta3AbstractArchiveImpl.__subclasses__():
            if clazz.can_open(archive_path):
                return clazz(archive_path)

        raise RuntimeError("Error! Archive %s bad or not supported!" % archive_path)


class XDelta3AbstractArchiveImpl(object):
    def __init__(self):
        self.lock = threading.RLock()

    def _acquire_lock(self):
        self.lock.acquire()

    def _release_lock(self):
        self.lock.release()

    def list_items(self):
        assert self.members

        return self.members


class XDelta3FsImpl(XDelta3AbstractArchiveImpl):
    def __init__(self, path, for_writing=False):
        super().__init__()

        self.path = path
        self._members = None

        # Pre-fetch member data to ensure only one thread tries to create
        # the initial list and to allow further listings to not need locks
        if not for_writing:
            self.list_items()

    # Placeholders to allow us to use 'with` keywords on this implementation
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass

    def close(self):
        pass

    @staticmethod
    def can_open(archive):
        return path.isdir(archive)

    def _add_listing_object(self, dir_listing, method, absolute_path):
        uid = lstat(absolute_path).st_uid
        gid = lstat(absolute_path).st_gid
        mode = S_IMODE(lstat(absolute_path).st_mode)

        group = None
        try:
            group = getgrgid(gid)[0]
        except KeyError as ke:
            pass
        except NameError as ne:
            pass

        user = None
        try:
            user = (getpwuid(uid)[0],)
        except KeyError as ke:
            pass
        except NameError as ne:
            pass

        setter_func = getattr(dir_listing, method)

        file_obj = setter_func(
            path.basename(absolute_path),
            absolute_path,
            mode,
            user,
            uid,
            group,
            gid,
            path.islink(absolute_path),
        )

        if file_obj and file_obj.is_link:
            file_obj.link_target = readlink(absolute_path)

        return file_obj

    # XXX: Not thread safe when uninitialized
    @property
    def members(self):
        if self._members:
            return self._members

        print("FS: Gathering filelist (%s/)" % path.basename(self.path))
        member_tree = {}
        member_tree["."] = DirListing(path.basename(self.path))
        for root, dirs, filenames in walk(self.path):
            relative_path = path.relpath(root, self.path)

            current_dir = DirListing()
            member_tree[relative_path] = current_dir

            parent_dir = path.dirname(relative_path)
            if root != self.path and not parent_dir:
                parent_dir = "."

            self._add_listing_object(current_dir, "set_metadata", root)

            if parent_dir in member_tree:
                member_tree[parent_dir].add_subdir(current_dir)

            files_to_process = filenames
            for directory in dirs:
                if path.islink(path.join(root, directory)):
                    files_to_process.append(directory)

            for filename in files_to_process:
                absolute_path = path.join(root, filename)
                relative_path = path.relpath(absolute_path, self.path)

                # Have the listing for the file in the map but
                # don't associate a DirListing object to it
                file_dict = self._add_listing_object(
                    current_dir, "add_file", absolute_path
                )

                member_tree[relative_path] = file_dict

        # Make sure that the root has a real unique value rather than '.'
        member_tree[None] = member_tree.pop(".")

        print("FS: Gathering completed (%s/)" % path.basename(self.path))

        self._members = member_tree

        return self._members

    def expand(self, root, extraction_path):
        assert root in self.members, "Unknown member path specified: %s" % root

        root_obj = self.members[root]

        if not root:
            root = "."

        source_path = path.join(self.path, root)
        target_path = path.join(extraction_path, root)

        dir_path = path.dirname(target_path)

        if root_obj.is_link:
            makedirs(dir_path, exist_ok=True)

            source_path = path.abspath(source_path)
            target_path = path.abspath(target_path)

            symlink(root_obj.link_target, target_path)
        elif root_obj.is_file:
            makedirs(dir_path, exist_ok=True)
            copy2(source_path, target_path, follow_symlinks=False)
        else:
            makedirs(target_path, exist_ok=True)

            # TODO: Test me
            # Ensure that permissions/ids are transferred along to the target
            copymode(source_path, target_path)
            copystat(source_path, target_path)

            try:
                lchown(target_path, root_obj.uid, root_obj.gid)
            except PermissionError as pe:
                # XXX: We can't copy the uid/gid unless we're run as root
                #      which means that in most cases we can't get that
                #      included in the diffs unless we add direct linkage
                #      between archive<->archive to copy these values

                # print('WARNING! Could not change uid/gid of', target_path)
                pass
            except NameError as ne:
                pass

    def create(self, base_dir):
        if path.isdir(self.path):
            raise Exception("Error! Archive already present!")

        copytree(base_dir, self.path, symlinks=True, ignore_dangling_symlinks=False)


class XDelta3TarImpl(XDelta3AbstractArchiveImpl):
    TAR_FORMAT = "gz"

    def __init__(self, archive_path, for_writing=False):
        super().__init__()

        self._items = None
        flags = "r:*"

        if for_writing:
            if path.isfile(archive_path):
                raise Exception("Error! Archive already present!")

            flags = "w:%s" % self.TAR_FORMAT

        self.archive_object = tarfile.open(archive_path, flags)
        self.archive_name = path.basename(archive_path)

        # Pre-fetch member data to ensure only one thread tries to create
        # the initial list and to allow further listings to not need locks
        if not for_writing:
            self.list_items()

    def _close_archive(self):
        self.archive_object.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._close_archive()

    def close(self):
        self._close_archive()

    @staticmethod
    def can_open(archive):
        return path.isfile(archive) and tarfile.is_tarfile(archive)

    def _add_listing_object(self, dir_listing, method, member):
        setter_func = getattr(dir_listing, method)

        file_obj = setter_func(
            path.basename(member.name),
            member,
            member.mode,
            member.uname,
            member.uid,
            member.gname,
            member.gid,
            member.issym(),
        )

        if file_obj and file_obj.is_link:
            file_obj.link_target = member.linkname

        return file_obj

    def _create_dir_structure_to(self, items, target_path):
        target_path_segments = target_path.split(path.sep)
        for index, segment in enumerate(target_path_segments):
            segment_path = path.sep.join(target_path_segments[0 : index + 1])

            # Skip paths that are in hierarchy
            if segment_path in items:
                continue

            parent_obj = items[None]
            if index != 0:
                parent_obj = items[path.dirname(segment_path)]

            subdir_obj = DirListing(segment)
            parent_obj.add_subdir(subdir_obj)

            items[segment_path] = subdir_obj

    def _safe_makedirs(self, target_dir):
        super()._acquire_lock()
        makedirs(target_dir, exist_ok=True)
        super()._release_lock()

    # XXX: Not thread safe when uninitialized
    @property
    def members(self):
        if self._items:
            return self._items

        print("Tar: Gathering filelist (%s)" % self.archive_name)
        members = self.archive_object.getmembers()

        # Lookup all member objects. Order is not ensured so we
        # need to do a 2-pass run to assign the proper hierarchy
        # to DirListing objects and files
        folders = []
        files = []
        for member in members:
            if member.isdir():
                folders.append((member.name.rstrip(path.sep), member))
            else:
                files.append((member.name, member))

        # Sort the directories - we want to navigate from trunk to leaf nodes
        sorted_dirs = sorted(folders, key=lambda f: len(f[0]))

        # Create the folder structure
        items = {None: DirListing(self.archive_name)}
        for folder, member in sorted_dirs:
            parent_dir = path.dirname(folder)
            if not parent_dir:
                parent_dir = None
            current_dir = DirListing()

            self._add_listing_object(current_dir, "set_metadata", member)

            items[folder] = current_dir

            if parent_dir in items:
                items[parent_dir].add_subdir(current_dir)

        # Add files to dirs
        for filename, member in files:
            dir_name = path.dirname(filename)
            if not dir_name:
                dir_name = None

            # Create the missing structure if needed
            if dir_name not in items.keys():
                self._create_dir_structure_to(items, dir_name)

            current_dir = items[dir_name]
            file_obj = self._add_listing_object(current_dir, "add_file", member)

            items[filename] = file_obj

        print("Tar: Gathering completed (%s)" % self.archive_name)

        # Create an ordered list rather than a regular dictionary
        # since tar archives are intended to be read sequentially
        ordered_items = OrderedDict()
        ordered_items[None] = items[None]
        for item in members:
            ordered_items[item.name] = items.pop(item.name.rstrip(path.sep))

        # Add back any items that we manually created (missing hierarchy)
        ordered_items.update(items)

        self._items = ordered_items

        return self._items

    def _expand_children(self, root, extraction_path):
        file_obj = self.members[root]

        self._safe_makedirs(extraction_path)

        for item in file_obj.dirs + file_obj.files:
            internal_path = item.name
            if root != None:
                internal_path = path.join(root, item.name)

            self.expand(internal_path, extraction_path)

    def expand(self, root, extraction_path):
        assert root in self.members, "Unknown member path specified: %s" % root

        file_obj = self.members[root]

        if not root:
            self._expand_children(None, extraction_path)
            return

        member = None
        if file_obj.data:
            member = file_obj.data
        else:
            # This is for folders that are returned by list_items which
            # don't have a matching folder within the archive
            folder_path = path.join(extraction_path, root)

            self._safe_makedirs(folder_path)

            self._expand_children(root, extraction_path)

            # TODO: Move this to end of extraction
            # XXX: Does not do anything right now
            if member:
                chmod(folder_path, file_obj.mode, follow_symlinks=False)
                utime(folder_path, (member.mtime, member.mtime), follow_symlinks=False)

                try:
                    lchown(folder_path, file_obj.uid, file_obj.gid)
                except PermissionError as pe:
                    pass
                except NameError as ne:
                    pass

            return

        if not path.lexists(extraction_path):
            makedirs(extraction_path, exist_ok=True)

        # Manually handle symlinks
        if file_obj.is_link:
            link_path = path.join(extraction_path, root)
            target_dir = path.dirname(link_path)
            self._safe_makedirs(target_dir)

            symlink(file_obj.link_target, link_path)
        elif file_obj.is_dir:
            target_dir = path.join(extraction_path, root)
            self._safe_makedirs(target_dir)
        else:
            # XXX: Not thread safe http://bugs.python.org/issue23649
            super()._acquire_lock()
            try:
                self.archive_object.extract(member, extraction_path)
            finally:
                super()._release_lock()

    # TODO: Copy uid/gid/permissions from source folder into records
    def create(self, base_dir):
        for item in listdir(base_dir):
            item_path = path.join(base_dir, item)
            self.archive_object.add(item_path, item)


class XDelta3ZipImpl(XDelta3AbstractArchiveImpl):
    def __init__(self, archive_path, for_writing=False):
        super().__init__()

        self._members = None

        flags = "r"

        if for_writing:
            if path.isfile(archive_path):
                raise Exception("Error! Archive already present!")

            flags = "w"

        self.archive_object = zipfile.ZipFile(archive_path, flags)
        self.archive_path = archive_path

        # Pre-fetch member data to ensure only one thread tries to create
        # the initial list and to allow further listings to not need locks
        if not for_writing:
            self.list_items()

    def _close_archive(self):
        self.archive_object.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._close_archive()

    def close(self):
        self._close_archive()

    @staticmethod
    def can_open(archive):
        return path.isfile(archive) and zipfile.is_zipfile(archive)

    def _add_listing_object(self, dir_listing, method, name):
        zip_obj = self.archive_object.getinfo(name)
        setter_func = getattr(dir_listing, method)

        basename = ""
        filename = zip_obj.filename
        if filename.endswith(path.sep):
            basename = path.basename(filename.rstrip(path.sep))
        else:
            basename = path.basename(filename)

        return setter_func(basename, zip_obj, None, None, None, None, None, False)

    # XXX: Not thread safe when uninitialized
    @property
    def members(self):
        if self._members:
            return self._members

        items = {}

        print("Zip: Gathering filelist (%s)" % path.basename(self.archive_path))

        # Sorted to ensure that we do top-down traversal
        sorted_names = sorted(self.archive_object.namelist(), key=len)

        items[None] = DirListing(path.basename(self.archive_path))

        for name in sorted_names:
            if name.endswith(path.sep):
                fixed_name = name.rstrip(path.sep)
                dir_listing = DirListing()

                items[fixed_name] = dir_listing

                self._add_listing_object(dir_listing, "set_metadata", name)

                parent_dir = path.dirname(fixed_name)
                if not parent_dir:
                    parent_dir = None

                items[parent_dir].add_subdir(dir_listing)
            else:
                dir_name = path.dirname(name)
                if not dir_name:
                    dir_name = None

                dir_listing = items[dir_name]

                file_obj = self._add_listing_object(dir_listing, "add_file", name)
                items[name] = file_obj

        print("Zip: Gathering completed (%s)" % path.basename(self.archive_path))

        # Intentional separation of variables so that we don't assign
        # some garbage value to self.items as well as print out the debug
        # messages
        self._members = items

        return self._members

    def expand(self, root, extraction_path):
        assert root in self.members, "Unknown member path specified: %s" % root

        if not path.isdir(extraction_path):
            makedirs(extraction_path, exist_ok=True)

        self.archive_object.extract(self.members[root].data, extraction_path)

    def create(self, base_dir):
        for root, dirnames, filenames in walk(base_dir):
            for filename in filenames:
                full_path = path.join(root, filename)
                internal_path = path.relpath(full_path, base_dir)

                self.archive_object.write(full_path, internal_path)


# ---------------------------- XDELTA3 ADAPTER ----------------------------
class XDelta3Impl(object):
    # TODO: Unit test me
    @staticmethod
    def run_command(args, exec_method=check_output):
        try:
            output = exec_method(args, stderr=STDOUT, universal_newlines=True)
        except CalledProcessError as cpe:
            print()
            print("XDELTA FAIL:", cpe.returncode, cpe.output)
            print()

            raise (cpe)

    @staticmethod
    def _print_command(prefix, command):
        command_line = prefix
        for arg in command:
            if " " in arg:
                command_line += " '" + arg + "'"
            else:
                command_line += " " + arg

        print(command_line)
        stdout.flush()

    # TODO: Test me
    @staticmethod
    def diff(old_file, new_file, target_file, debug=False):
        command = ["lib/xdelta3", "-f", "-e"]
        if old_file:
            command.append("-s")
            command.append(old_file)

        command.append(new_file)
        command.append(target_file)

        if debug:
            XDelta3Impl._print_command("XD Diff:", command)

        XDelta3Impl.run_command(command)

    # TODO: Test me
    @staticmethod
    def apply(old_file, patch_file, target_file, debug=False):
        command = ["lib/xdelta3", "-f", "-d"]
        if old_file:
            command.append("-s")
            command.append(old_file)

        command.append(patch_file)
        command.append(target_file)

        if debug:
            XDelta3Impl._print_command("XD Apply:", command)

        XDelta3Impl.run_command(command)


# ---------------------------- MAIN CLASS ----------------------------
class XDelta3DirPatcher(object):
    PATCH_FOLDER = "xdelta"
    METADATA_FILE = ".info"

    def __init__(self, args, delta_impl=XDelta3Impl):
        self.args = args
        self.delta_impl = delta_impl

    # TODO: Unit test me
    def copy_attributes(self, src_file, dest_file):
        if self.args.verbose:
            print("Copying file metadata:", dest_file)
        copymode(src_file, dest_file)
        copystat(src_file, dest_file)

        uid = stat(src_file).st_uid
        gid = stat(src_file).st_gid

        try:
            lchown(dest_file, uid, gid)
        except NameError as ne:
            pass

    # TODO: Unit test me
    def copy_attributes_from_archive(self, archive_object, filename, target):
        if self.args.verbose:
            print("Copying file metadata (archive):", filename)
        file_obj = archive_object.list_items()[filename]

        if file_obj.permissions:
            chmod(target, file_obj.permissions)

        if file_obj.uid and file_obj.gid:
            try:
                lchown(target, file_obj.uid, file_obj.gid)
            except PermissionError as pe:
                # We only ignore problems here if ignore_euid flag is set
                if not self.args.ignore_euid:
                    raise pe
            except NameError as ne:
                pass

    def _find_file_delta(
        self,
        filename,
        old_archive_obj,
        new_archive_obj,
        old_root,
        new_root,
        target_root,
    ):
        if self.args.debug:
            print("Processing '%s'" % filename)
        else:
            print("#", end="")
        stdout.flush()

        new_archive_obj.expand(filename, new_root)

        if filename in old_archive_obj.list_items().keys():
            old_archive_obj.expand(filename, old_root)

        old_path = path.join(old_root, filename)
        new_path = path.join(new_root, filename)
        target_path = path.join(target_root, filename)
        target_dir = path.dirname(target_path)

        if self.args.debug:
            print("Diff:", old_path, new_path, target_path)

        if path.islink(path.abspath(new_path)):
            source_path = path.abspath(new_path)
            dest_path = path.abspath(target_path)

            target_dir = path.dirname(dest_path)
            if not path.lexists(target_dir):
                makedirs(target_dir, exist_ok=True)

            new_dst = readlink(source_path)
            symlink(new_dst, dest_path)
            if self.args.debug:
                print("symlink: ", [source_path, dest_path])

        elif path.isdir(new_path):
            if not path.lexists(target_dir):
                makedirs(target_dir, exist_ok=True)

            self.copy_attributes(new_path, target_dir)
        else:
            if not path.lexists(target_dir):
                makedirs(target_dir, exist_ok=True)

            # Regular file
            if not path.isfile(old_path):
                old_path = None
                if self.args.debug:
                    print("Old file not present. Ignoring source in XDelta")

            self.delta_impl.diff(old_path, new_path, target_path, self.args.debug)

            self.copy_attributes(new_path, target_path)

        # Remove each individual file as they're processed
        # to reduce needed size on-disk
        for item in [old_path, new_path]:
            if item and (path.isfile(item) or path.islink(item)):
                remove(item)

    def _apply_file_delta(
        self,
        archive_object,
        patch_file,
        old_root,
        target_root,
        delta_patch_root,
        staging_dir,
    ):
        if self.args.debug:
            print("Processing '%s'" % patch_file)
        else:
            print("#", end="")
        stdout.flush()

        archive_object.expand(patch_file, staging_dir)

        rel_path = path.relpath(patch_file, delta_patch_root)
        old_path = path.join(old_root, rel_path)
        patch_path = path.join(staging_dir, patch_file)
        target_path = path.join(target_root, rel_path)

        target_dir = path.dirname(target_path)
        if args.debug:
            print("Apply:", old_path, patch_path, target_path)

        if path.islink(patch_path):
            if path.normpath(target_path) != target_dir and not path.isdir(target_dir):
                if args.debug:
                    print("Creating parent of a symlink:", target_dir)
                makedirs(target_dir, exist_ok=True)

            patch_dst = readlink(patch_path)
            symlink(patch_dst, target_path)
            if args.debug:
                print("symlink: ", [target_path, patch_dst])

        elif path.isdir(patch_path):
            makedirs(target_path, exist_ok=True)
            self.copy_attributes_from_archive(archive_object, patch_file, target_path)
        else:
            makedirs(target_dir, exist_ok=True)

            # Regular file
            if not path.isfile(old_path):
                if args.debug:
                    print("File missing: '%s'." "Ignoring source in XDelta" % old_path)
                old_path = None

            self.delta_impl.apply(old_path, patch_path, target_path, self.args.debug)

            self.copy_attributes_from_archive(archive_object, patch_file, target_path)

            remove(patch_path)

    # TODO: Unit test me
    def diff(
        self,
        old_dir,
        new_dir,
        patch_bundle,
        metadata=None,
        staging_dir=None,
        runner=ExecutorRunner(),
    ):
        target_dir = mkdtemp(
            prefix="%s_target" % XDelta3DirPatcher.__name__, dir=staging_dir
        )

        print("Using '%s' as staging area" % target_dir)
        stdout.flush()

        delta_target_dir = path.join(target_dir, self.PATCH_FOLDER)
        if not path.isdir(delta_target_dir):
            mkdir(delta_target_dir)

        with XDeltaArchive(old_dir) as old_archive_obj, XDeltaArchive(
            new_dir
        ) as new_archive_obj:
            old_staging_dir = mkdtemp(
                prefix="%s_old_src" % XDelta3DirPatcher.__name__, dir=staging_dir
            )
            new_staging_dir = mkdtemp(
                prefix="%s_new_src" % XDelta3DirPatcher.__name__, dir=staging_dir
            )

            for filename in new_archive_obj.list_items().keys():
                if not filename:
                    continue

                if self.args.debug:
                    print("Queueing '%s'" % filename)
                else:
                    print(".", end="")
                stdout.flush()

                runner.add_task(
                    self._find_file_delta,
                    (
                        filename,
                        old_archive_obj,
                        new_archive_obj,
                        old_staging_dir,
                        new_staging_dir,
                        delta_target_dir,
                    ),
                )

            # Wait until we diffed everything
            runner.join_all()

        # FIXME: Figure out how to handle dirs (premissions/uids/etc)

        rmtree(old_staging_dir)
        rmtree(new_staging_dir)

        # TODO: Delegate this to archive impl
        print("\nWriting archive...")
        with tarfile.open(
            patch_bundle, "w:gz", format=tarfile.GNU_FORMAT
        ) as patch_archive:
            patch_archive.add(delta_target_dir, arcname=self.PATCH_FOLDER)

            if metadata:
                print("Adding metadata (.info)")
                patch_archive.add(metadata, arcname=self.METADATA_FILE)

        print("Cleaning up...")
        rmtree(target_dir)

        print("Done")

    # XXX This implementation needs directories passed in to be
    #     empty for proper cleanup. Callers are required to send
    #     in files first if they expect a mixture of dirs/files
    #     on the same paths.
    @staticmethod
    def remove_item(target_dir, deleted_item, debug=False, attempt=0):
        deleted_item_path = path.join(target_dir, deleted_item)
        if debug:
            print("Deleting '%s'" % deleted_item_path)
        else:
            print("X", end="")

        if not path.lexists(deleted_item_path):
            return

        if path.isdir(deleted_item_path):
            try:
                rmdir(deleted_item_path)
            except OSError as e:
                # We don't care about directories that might get leftover
                # since they will be empty anyways but we do our best to
                # clean up
                pass
        else:
            remove(deleted_item_path)

    # TODO: Unit test me
    def apply(
        self,
        old_dir,
        patch_bundle,
        target_dir,
        root_patch_dir=None,
        staging_dir=None,
        runner=ExecutorRunner(),
    ):
        in_place_apply = old_dir == target_dir

        # TODO: Test me
        # Create a temp dir
        patch_staging_dir = mkdtemp(
            prefix="%s_delta_expanded" % XDelta3DirPatcher.__name__, dir=staging_dir
        )
        print("Using '%s' as staging area" % patch_staging_dir)

        # If we want to apply only a part of the xdelta files
        # from withing the delta bundle xdelta/ folder
        if not root_patch_dir:
            delta_patch_root = self.PATCH_FOLDER
        else:
            delta_patch_root = path.join(self.PATCH_FOLDER, root_patch_dir)

        # Make target dir if we don't have one
        if not path.isdir(target_dir):
            print("WARNING: Target directory not present so it will be created.")
            print(
                "  - Please ensure that the toplevel target dir has the correct permissions."
            )
            makedirs(target_dir)

        print("Applying patches from %s" % patch_bundle)
        with XDeltaArchive(old_dir) as old_archive, XDeltaArchive(
            patch_bundle
        ) as patch_archive:
            all_archive_items = patch_archive.list_items().keys()

            if self.args.verbose:
                print("All in patch: %s" % all_archive_items)

            patches = [
                p for p in all_archive_items if p and p.startswith(delta_patch_root)
            ]
            if self.args.verbose:
                print("Patches: %s" % patches)

            # XXX: We need to strip out the internal path prefix on the patches
            #      to be able to compare the file lists and build a "to_remove"
            #      array. The reason why we use len() vs path.relpath() is so that
            #      we don't strip out the trailing path.sep() on directories that
            #      relpath does automatically
            files_in_patch = []
            for filename in patches:
                relative_filename = filename[len(delta_patch_root) + len(path.sep) :]
                files_in_patch.append(relative_filename)

            if self.args.verbose:
                print("In patch: %s" % files_in_patch)

            removed_items = []
            for old_file in old_archive.list_items().keys():
                if old_file and old_file not in files_in_patch:
                    removed_items.append(old_file)

            # XXX: This is to ensure that we never try to remove a directory while
            #      all of its children are in queue waiting for a thread (causing
            #      a deadlock)
            removed_items.sort(key=len, reverse=True)

            if self.args.verbose:
                print("Removed: %s" % removed_items)

            # TODO: Verify that old archive has the expected files before we start

            print("Removing deleted files")
            for removed_item in removed_items:
                if self.args.debug:
                    print("Queueing(rm) '%s'" % removed_item)
                else:
                    print("x", end="")

                runner.add_task(
                    self.remove_item, (target_dir, removed_item, self.args.debug)
                )

            for patch in patches:
                if self.args.debug:
                    print("Queueing '%s'" % patch)
                else:
                    print(".", end="")
                stdout.flush()

                runner.add_task(
                    self._apply_file_delta,
                    (
                        patch_archive,
                        patch,
                        old_dir,
                        target_dir,
                        delta_patch_root,
                        patch_staging_dir,
                    ),
                )
            runner.join_all()

        print("Cleaning up")
        rmtree(patch_staging_dir)

        print("Done")

    @staticmethod
    def check_euid(ignore_euid, get_euid_method=None):
        if not get_euid_method:
            if os_name != "nt":
                get_euid_method = geteuid
            else:
                get_euid_method = lambda: None

        if (not ignore_euid) and get_euid_method() != 0:
            print >> sys.stderr, "ERROR: You must be root to apply the delta!" "Exiting.\n"
            raise Exception()

    def run(self):
        print("Running directory patcher...")

        if self.args.action == "diff":
            if self.args.debug:
                print("Parsing arguments")

            print("Generating delta pack")
            self.diff(
                self.args.old_version,
                self.args.new_version,
                self.args.patch_bundle,
                self.args.metadata,
                self.args.staging_dir,
            )
        else:
            # If we're not the root user, bail since we can't ensure that the
            # user and group permissions are retained
            self.check_euid(self.args.ignore_euid)

            # If no target dir specified, assume in-place update
            if not self.args.target_dir:
                self.args.target_dir = self.args.old_dir

            print("Applying delta pack")
            self.apply(
                self.args.old_dir,
                self.args.patch_bundle,
                self.args.target_dir,
                self.args.root_patch_dir,
                self.args.staging_dir,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Creates and applies XDelta3-based directory diff archive files"
    )

    subparsers = parser.add_subparsers(dest="action")
    parser_apply = subparsers.add_parser(
        "apply",
        help='Apply a diff from a directory. See "apply -help" for more options',
    )

    parser_diff = subparsers.add_parser(
        "diff",
        help='Generate a diff from directories/files. See "diff -help" for more options',
    )

    # Arguments to apply a diff
    parser_apply.add_argument(
        "old_dir", help="Folder/archive containing the old version of the files"
    )

    parser_apply.add_argument("patch_bundle", help="Archive containing the patches")

    parser_apply.add_argument(
        "target_dir",
        nargs="?",
        default=None,
        help="Destination folder/archive for the new versions of files",
    )

    parser_apply.add_argument(
        "-d",
        "--root-patch-dir",
        nargs="?",
        default=None,
        help="Root directory from the diff bundle from where to apply \
                  the patches",
    )

    parser_apply.add_argument(
        "--ignore-euid",
        help="Disable checking of EUID on applying the patch",
        default=False,
        action="store_true",
    )

    # Arguments to create a diff
    parser_diff.add_argument(
        "-m",
        "--metadata",
        nargs="?",
        default=None,
        help="Add this file (renamed to .info) as metadata to the diff",
    )

    parser_diff.add_argument(
        "old_version", help="Folder or archive containing the old version of the files"
    )

    parser_diff.add_argument(
        "new_version", help="Folder or archive containing the new version of the files"
    )

    parser_diff.add_argument(
        "patch_bundle", help="Destination path for the generated patch diff"
    )

    # Generic arguments
    parser.add_argument(
        "-s",
        "--staging-dir",
        nargs="?",
        default=None,
        help="Use this directory for all staging output of this program. Defaults to /tmp.",
    )

    parser.add_argument("--debug", help="Enable debugging output", action="store_true")

    parser.add_argument(
        "--verbose",
        help="Enable extremely verbose debugging output",
        action="store_true",
    )

    parser.add_argument("--version", action="version", version="%(prog)s v" + VERSION)

    args = AttributeDict(vars(parser.parse_args()))

    if args.verbose:
        args.debug = True

    if args.action:
        XDelta3DirPatcher(args).run()
    else:
        parser.print_help()
