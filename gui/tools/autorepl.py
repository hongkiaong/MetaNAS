#!/usr/bin/env python
#-
# Copyright (c) 2012 MetaComplex, Corp.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#

import datetime
import os
import sys
import syslog

sys.path.extend([
    '/usr/local/www',
    '/usr/local/www/metanasUI',
])

from django.core.management import setup_environ
from metanasUI import settings
setup_environ(settings)

from metanasUI.storage.models import Replication
from metanasUI.common.pipesubr import pipeopen, setname, system
from metanasUI.common.locks import mntlock
from metanasUI.common.system import send_mail

# DESIGN NOTES
#
# A snapshot transists its state in its lifetime this way:
#   NEW:                        A newly created snapshot by autosnap
#   LATEST:                     A snapshot marked to be the latest one
#   -:                          The replication system no longer cares this.
#

# Set to True if verbose log desired
debug = False

# Detect if another instance is running
def exit_if_running(pid):
    syslog.syslog(syslog.LOG_DEBUG,
                  "Checking if process %d is still alive" % (pid, ))
    try:
        os.kill(pid, 0)
        # If we reached here, there is another process in progress
        syslog.syslog(syslog.LOG_DEBUG,
                      "Process %d still working, quitting" % (pid, ))
        sys.exit(0)
    except OSError:
        syslog.syslog(syslog.LOG_DEBUG, "Process %d gone" % (pid, ))

MNTLOCK = mntlock()
setname('autorepl')
syslog.openlog("autorepl", syslog.LOG_CONS | syslog.LOG_PID)

sshcmd = '/usr/bin/ssh -i /data/ssh/replication -o BatchMode=yes -o StrictHostKeyChecking=yes -q'

mypid = os.getpid()
templog = '/tmp/repl-%d' % (mypid)

# (mis)use MNTLOCK as PIDFILE lock.
locked = True
try:
    MNTLOCK.lock_try()
except IOError:
    locked = False
if not locked:
    sys.exit(0)

AUTOREPL_PID = -1
try:
    with open('/var/run/autorepl.pid') as pidfile:
        AUTOREPL_PID = int(pidfile.read())
except:
    pass

if AUTOREPL_PID != -1:
    exit_if_running(AUTOREPL_PID)

with open('/var/run/autorepl.pid', 'w') as pidfile:
    pidfile.write('%d' % mypid)

MNTLOCK.unlock()

# At this point, we are sure that only one autorepl instance is running.

syslog.syslog(syslog.LOG_DEBUG, "Autosnap replication started")
syslog.syslog(syslog.LOG_DEBUG, "temp log file: %s" % (templog))

# Traverse all replication tasks
replication_tasks = Replication.objects.all()
for replication in replication_tasks:
    remote = replication.repl_remote.ssh_remote_hostname.__str__()
    remote_port = replication.repl_remote.ssh_remote_port
    remotefs = replication.repl_zfs.__str__()
    localfs = replication.repl_filesystem.__str__()
    last_snapshot = replication.repl_lastsnapshot.__str__()
    resetonce = replication.repl_resetonce

    if replication.repl_userepl:
        Rflag = '-R '
    else:
        Rflag = ''

    wanted_list = []
    known_latest_snapshot = ''
    expected_local_snapshot = ''

    localfs_split = localfs.split('/')

    if len(localfs_split) > 1:
        remotefs_final = "%s/%s" % (remotefs, "/".join(localfs_split[1:]))
        if len(localfs_split) > 2:
            remotefs_parent = "%s/%s" % (remotefs, "/".join(localfs_split[1:-1]))
        else:
            remotefs_parent = "%s/%s" % (remotefs, localfs_split[1])
    else:
        remotefs_final = remotefs
        remotefs_parent = remotefs

    # Test if there is work to do, if so, own them
    MNTLOCK.lock()
    syslog.syslog(syslog.LOG_DEBUG, "Checking dataset %s" % (localfs))
    zfsproc = pipeopen('/sbin/zfs list -Ht snapshot -o name,metanas:state -r -S creation -d 1 %s' % (localfs), debug)
    output, error = zfsproc.communicate()
    if zfsproc.returncode:
        syslog.syslog(syslog.LOG_ALERT,
            'Could not determine last available snapshot for dataset %s: %s'
            % (localfs, error, ))
        MNTLOCK.unlock()
        continue
    if output != '':
        snapshots_list = output.split('\n')
        found_latest = False
        for snapshot_item in snapshots_list:
            if snapshot_item != '':
                snapshot, state = snapshot_item.split('\t')
                if found_latest:
                    # assert (known_latest_snapshot != '') because found_latest
                    if state != '-':
                        system('/sbin/zfs set metanas:state=NEW %s' % (known_latest_snapshot))
                        system('/sbin/zfs set metanas:state=LATEST %s' % (snapshot))
                        wanted_list.insert(0, known_latest_snapshot)
                        syslog.syslog(syslog.LOG_DEBUG, "Snapshot %s added to wanted list (was LATEST)" % (snapshot))
                        known_latest_snapshot = snapshot
                        syslog.syslog(syslog.LOG_ALERT, "Snapshot %s became latest snapshot" % (snapshot))
                else:
                    syslog.syslog(syslog.LOG_DEBUG, "Snapshot: %s State: %s" % (snapshot, state))
                    if state == 'LATEST' and not resetonce:
                        found_latest = True
                        known_latest_snapshot = snapshot
                        syslog.syslog(syslog.LOG_DEBUG, "Snapshot %s is the recorded latest snapshot" % (snapshot))
                    elif state == 'NEW' or resetonce:
                        wanted_list.insert(0, snapshot)
                        syslog.syslog(syslog.LOG_DEBUG, "Snapshot %s added to wanted list" % (snapshot))
                    elif state.startswith('INPROGRESS'):
                        # For compatibility with older versions
                        wanted_list.insert(0, snapshot)
                        system('/sbin/zfs set metanas:state=NEW %s' % (snapshot))
                        syslog.syslog(syslog.LOG_DEBUG, "Snapshot %s added to wanted list (stale)" % (snapshot))
                    elif state == '-':
                        # The snapshot is already replicated, or is not
                        # an automated snapshot.
                        syslog.syslog(syslog.LOG_DEBUG, "Snapshot %s unwanted" % (snapshot))
                    else:
                        # This should be exception but skip for now.
                        MNTLOCK.unlock()
                        continue
    MNTLOCK.unlock()

    # If there is nothing to do, go through next replication entry
    if len(wanted_list) == 0:
        continue

    if known_latest_snapshot != '' and not resetonce:
        # Check if it matches remote snapshot
        rzfscmd = '"zfs list -Hr -o name -S creation -t snapshot -d 1 %s | head -n 1 | cut -d@ -f2"' % (remotefs_final)
        sshproc = pipeopen('%s -p %d %s %s' % (sshcmd, remote_port, remote, rzfscmd))
        output = sshproc.communicate()[0]
        if output != '':
            expected_local_snapshot = '%s@%s' % (localfs, output.split('\n')[0])
            if expected_local_snapshot == last_snapshot:
                # Accept: remote and local snapshots matches
                syslog.syslog(syslog.LOG_DEBUG, "Found matching latest snapshot %s remotely" % (last_snapshot))
            elif expected_local_snapshot == known_latest_snapshot:
                # Accept
                syslog.syslog(syslog.LOG_DEBUG, "Found matching latest snapshot %s remotely (but not the recorded one)" % (known_latest_snapshot))
                last_snapshot = known_latest_snapshot
            else:
                # Do we have it locally? if yes then mark it immediately
                syslog.syslog(syslog.LOG_INFO, "Can not locate expected snapshot %s, looking more carefully" % (expected_local_snapshot))
                MNTLOCK.lock()
                zfsproc = pipeopen('/sbin/zfs list -Ht snapshot -o name,metanas:state %s' % (expected_local_snapshot), debug)
                output = zfsproc.communicate()[0]
                if output != '':
                    last_snapshot, state = output.split('\n')[0].split('\t')
                    syslog.syslog(syslog.LOG_INFO, "Marking %s as latest snapshot" % (last_snapshot))
                    if state == '-':
                        system('/sbin/zfs inherit metanas:state %s' % (known_latest_snapshot))
                        system('/sbin/zfs set metanas:state=LATEST %s' % (last_snapshot))
                        known_latest_snapshot = last_snapshot
                else:
                    syslog.syslog(syslog.LOG_ALERT, "Can not locate a proper local snapshot for %s" % (localfs))
                    # Can NOT proceed any further.  Report this situation.
                    error, errmsg = send_mail(subject="Replication failed!", text=\
                        """
Hello,
    The replication failed for the local ZFS %s because the remote system
    have diverged snapshot with us.
                        """ % (localfs), interval=datetime.timedelta(hours=2), channel='autorepl')
                    MNTLOCK.unlock()
                    continue
                MNTLOCK.unlock()
        else:
            syslog.syslog(syslog.LOG_NOTICE, "Can not locate %s on remote system, starting from there" % (known_latest_snapshot))
            # Reset the "latest" snapshot to a new one.
            system('/sbin/zfs set metanas:state=NEW %s' % (known_latest_snapshot))
            wanted_list.insert(0, known_latest_snapshot)
            last_snapshot = ''
            known_latest_snapshot = ''
    if resetonce:
        syslog.syslog(syslog.LOG_NOTICE, "Destroying remote %s" % (remotefs_final))
        destroycmd = '%s -p %d %s /sbin/zfs destroy -rRf %s' % (sshcmd, remote_port, remote, remotefs_final)
        system(destroycmd)
        known_latest_snapshot = ''
    if known_latest_snapshot == '':
        # Create remote filesystem
        syslog.syslog(syslog.LOG_NOTICE, "Creating %s on remote system" % (remotefs_parent))
        replcmd = '%s -p %d %s /sbin/zfs create -o readonly=on -p %s' % (sshcmd, remote_port, remote, remotefs_parent)
        system(replcmd)
        last_snapshot = ''
    else:
        last_snapshot = known_latest_snapshot

    for snapname in wanted_list:
        #if replication.repl_limit != 0:
        #    limit = ' | /usr/local/bin/throttle -K %d' % replication.repl_limit
        #else:
        #    limit = ''
        limit = ''
        if last_snapshot == '':
            replcmd = '(/sbin/zfs send %s%s%s | %s -p %d %s "/sbin/zfs receive -F -d %s && echo Succeeded.") > %s 2>&1' % (Rflag, snapname, limit, sshcmd, remote_port, remote, remotefs, templog)
        else:
            replcmd = '(/sbin/zfs send %s-I %s %s%s | %s -p %d %s "/sbin/zfs receive -F -d %s && echo Succeeded.") > %s 2>&1' % (Rflag, last_snapshot, snapname, limit, sshcmd, remote_port, remote, remotefs, templog)
        system(replcmd)
        with open(templog) as f:
            msg = f.read()
        os.remove(templog)
        syslog.syslog(syslog.LOG_DEBUG, "Replication result: %s" % (msg))

        # Determine if the remote side have the snapshot we have now.
        rzfscmd = '"zfs list -Hr -o name -S creation -t snapshot -d 1 %s | head -n 1 | cut -d@ -f2"' % (remotefs_final)
        sshproc = pipeopen('%s -p %d %s %s' % (sshcmd, remote_port, remote, rzfscmd))
        output = sshproc.communicate()[0]
        if output != '':
            expected_local_snapshot = '%s@%s' % (localfs, output.split('\n')[0])
            if expected_local_snapshot == snapname:
                system('%s -p %d %s "/sbin/zfs inherit -r metanas:state %s"' % (sshcmd, remote_port, remote, remotefs_final))
                # Replication was successful, mark as such
                MNTLOCK.lock()
                if last_snapshot != '':
                    system('/sbin/zfs inherit metanas:state %s' % (last_snapshot))
                last_snapshot = snapname
                system('/sbin/zfs set metanas:state=LATEST %s' % (last_snapshot))
                MNTLOCK.unlock()
                replication.repl_lastsnapshot = last_snapshot
                if resetonce:
                    replication.repl_resetonce = False
                replication.save()
                continue
            else:
                syslog.syslog(syslog.LOG_ALERT, "Remote and local mismatch after replication: %s vs %s" % (expected_local_snapshot, snapname))
                rzfscmd = '"zfs list -Ho name -t snapshot %s | head -n 1 | cut -d@ -f2"' % (remotefs_final)
                sshproc = pipeopen('%s -p %d %s %s' % (sshcmd, remote_port, remote, rzfscmd))
                output = sshproc.communicate()[0]
                if output != '':
                    expected_local_snapshot = '%s@%s' % (localfs, output.split('\n')[0])
                    if expected_local_snapshot == snapname:
                        syslog.syslog(syslog.LOG_ALERT, "Snapshot %s already exist on remote, marking as such" % (snapname))
                        system('%s -p %d %s "/sbin/zfs inherit -r metanas:state %s"' % (sshcmd, remote_port, remote, remotefs_final))
                        # Replication was successful, mark as such
                        MNTLOCK.lock()
                        system('/sbin/zfs inherit metanas:state %s' % (snapname))
                        MNTLOCK.unlock()
                        continue

        # Something wrong, report.
        syslog.syslog(syslog.LOG_ALERT, "Replication of %s failed with %s" % (snapname, msg))
        error, errmsg = send_mail(subject="Replication failed!", text=\
            """
Hello,
    The system was unable to replicate snapshot %s to %s
======================
%s
            """ % (localfs, remote, msg), interval=datetime.timedelta(hours=2), channel='autorepl')
        break

os.remove('/var/run/autorepl.pid')
syslog.syslog(syslog.LOG_DEBUG, "Autosnap replication finished")
