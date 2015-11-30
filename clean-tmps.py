#!/usr/bin/env python3


import enum
import fnmatch
from itertools import chain
import os
import shlex
import stat
import subprocess
import sys
import time


class Action(enum.Enum):
    skip = 0
    unlink = 1
    defer_rmdir_check = 2
    defer_unsymlink_check = 3


def process(path, stats):
    mode = stats.st_mode
    # Some file types (e.g. sockets) don't get their mtimes|atimes updated
    # when they're written|read. Always return False for those types
    # (except symlinks) and types we don't know about.
    if not any(is_(mode) for is_ in
               (stat.S_ISREG, stat.S_ISDIR, stat.S_ISLNK, stat.S_ISFIFO,
                stat.S_ISCHR)):
        return Action.skip
    # Symlinks' atimes aren't updated when they're used. Just return
    # Action.unlink or Action.defer_unsymlink_check for broken symlinks and
    # symlinks to old items, and leave the rest.
    if stat.S_ISLNK(mode):
        try:
            target_stats = os.stat(path, follow_symlinks=True)
        except FileNotFoundError:
            # The symlink is part of a long chain, part of a loop, or broken.
            # We'll treat those all the same.
            return Action.unlink
        target_action = process(None, target_stats)
        if target_action is Action.defer_rmdir_check:
            return Action.defer_unsymlink_check
        return target_action
    timestamps = [stats.st_mtime]
    if not stat.S_ISDIR(mode):
        timestamps.extend([stats.st_ctime, stats.st_atime])
    return (
        (Action.defer_rmdir_check if stat.S_ISDIR(mode) else Action.unlink)
        if all(timestamp <= THRESHOLD for timestamp in timestamps)
        else Action.skip
        )


try:
    already_running = int(os.environb.get(b'CLEAN_TMPS_RUNNING', False))
except ValueError:
    already_running = True

if not already_running:
    try:
        open('/etc/defaults/periodic.conf', 'r')
    except OSError:
        pass
    else:
        os.environb[b'CLEAN_TMPS_RUNNING'] = str(int(True)).encode('utf-8')
        command = (
            b'set -a;'
            b'. /etc/defaults/periodic.conf;'
            b'source_periodic_confs;' +
            os.fsencode(shlex.quote(sys.argv[0])) + b';'
            )
        sys.exit(subprocess.run([command], shell=True).returncode)

if os.environb.get(b'daily_clean_tmps_enable', b'no').lower() != b'yes':
    sys.exit(0)

days = os.environb.get(b'daily_clean_tmps_days', None)
if not days:
    # Printing these errors to stdout instead of stderr is behavior copied from
    # the /etc/periodic/daily/110.clean-tmps that originally shipped with OS X.
    # I'm not sure why they did it that way, but I'm gonna do it that way, too.
    print("$daily_clean_tmps_enable is set but $daily_clean_tmps_days is not")
    sys.exit(2)
try:
    days = int(days)
except ValueError:
    print("$daily_clean_tmps_days is not a valid integer")
    sys.exit(2)
if days <= 0:
    print("$daily_clean_tmps_days is not positive")
    sys.exit(2)
THRESHOLD = time.time() - days * 24 * 60 * 60

exclusions = os.environb.get(b'daily_clean_tmps_ignore', b'').split()
# No idea what this is about, but it was in the original script, so...
exclusions.append(b'.vfs_rsrc_streams_*')
verbose = os.environb.get(b'daily_clean_tmps_verbose', b'no').lower() == b'yes'
tmp_dirs = os.environb.get(b'daily_clean_tmps_dirs', b'').split()

# Some directories might become empty once we've deleted all the old files.
# Wait until the end to try deleting directories (but check their timestamps
# /before/ they get updated when we delete things!)
deferred_items = []

print()
print("Removing old temporary files:")

for tmp_dir in tmp_dirs:
    for dir_path, dir_names, file_names in os.walk(tmp_dir):
        for file_name in chain(file_names, dir_names):
            file_name = os.path.join(dir_path, file_name)
            if any(fnmatch.fnmatch(file_name, pattern) for pattern in exclusions):
                continue
            try:
                stats = os.stat(file_name, follow_symlinks=False)
            except OSError:
                # Can't stat() the file for some reason? Just ignore it, then.
                pass
            else:
                action = process(file_name, stats)
                if action is Action.unlink:
                    try:
                        os.unlink(file_name)
                    except OSError:
                        # Can't unlink() the file? Again, just ignore it.
                        pass
                    else:
                        if verbose:
                            sys.stdout.buffer.write(file_name)
                            sys.stdout.buffer.write(b'\n')
                elif action in {Action.defer_rmdir_check,
                                Action.defer_unsymlink_check}:
                    deferred_items.append((file_name, action))

# deferred_items was compiled in a top-down walk. Iterating over it in reverse
# gives us items in bottom-up order, so that we unlink children
# before their parents.
for path, action in reversed(deferred_items):
    try:
        if action is Action.defer_rmdir_check:
            os.rmdir(path)
        elif action is Action.defer_unsymlink_check:
            if not os.path.exists(path):
                os.unlink(path)
        # If this `else` looks redundant, go read the commit after ba2adb9d.
        else:
            continue
    except OSError:
        pass
    else:
        if verbose:
            sys.stdout.buffer.write(path)
            sys.stdout.buffer.write(b'\n')
