"""
Copyright (c) 2012-2013 RockStor, Inc. <http://rockstor.com>
This file is part of RockStor.

RockStor is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published
by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

RockStor is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""


import re
import pickle
import time
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import api_view
from django.db import transaction
from storageadmin.serializers import PoolInfoSerializer
from storageadmin.models import (Disk, Pool, Share, PoolBalance)
from fs.btrfs import (add_pool, pool_usage, resize_pool, umount_root,
                      btrfs_uuid, mount_root, start_balance, usage_bound,
                      remove_share)
from system.osi import remount, trigger_udev_update
from storageadmin.util import handle_exception
from django.conf import settings
import rest_framework_custom as rfc
from django_ztask.models import Task
import json

import logging
logger = logging.getLogger(__name__)


class PoolMixin(object):
    serializer_class = PoolInfoSerializer
    RAID_LEVELS = ('single', 'raid0', 'raid1', 'raid10', 'raid5', 'raid6')

    @staticmethod
    def _validate_disk(d, request):
        try:
            return Disk.objects.get(name=d)
        except:
            e_msg = ('Disk(%s) does not exist' % d)
            handle_exception(Exception(e_msg), request)

    @staticmethod
    def _role_filter_disk_names(disks, request):
        """
        Takes a series of disk objects and filters them based on their roles.
        For disk with a redirect role the role's value is substituted for that
        disks name. This effects a name re-direction for redirect role disks.
        N.B. Disk model now has sister code under Disk.target_name property.
        :param disks:  list of disks object
        :param request:
        :return: list of disk names post role filter processing
        """
        # TODO: Consider revising to use new Disk.target_name property.
        try:
            # Build dictionary of disks with roles
            role_disks = {d for d in disks if d.role is not None}
            # Build a dictionary of redirected disk names with their
            # associated redirect role values.
            # By using only role_disks we avoid json.load(None)
            redirect_disks = {d.name: json.loads(d.role).get("redirect", None)
                              for d in role_disks if
                              'redirect' in json.loads(d.role)}
            # Replace d.name with redirect role value for redirect role disks.
            # Our role system stores the /dev/disk/by-id name (without path)
            # for redirected disks so use that value instead as our disk name:
            dnames = [
                d.name if d.name not in redirect_disks else redirect_disks[
                    d.name] for d in disks]
            return dnames
        except:
            e_msg = ('Problem with role filter of disks' % disks)
            handle_exception(Exception(e_msg), request)

    @staticmethod
    def _validate_compression(request):
        compression = request.data.get('compression', 'no')
        if (compression is None):
            compression = 'no'
        if (compression not in settings.COMPRESSION_TYPES):
            e_msg = ('Unsupported compression algorithm(%s). Use one of '
                     '%s' % (compression, settings.COMPRESSION_TYPES))
            handle_exception(Exception(e_msg), request)
        return compression

    @staticmethod
    def _validate_mnt_options(request):
        mnt_options = request.data.get('mnt_options', None)
        if (mnt_options is None):
            return ''
        allowed_options = {
            'alloc_start': int,
            'autodefrag': None,
            'clear_cache': None,
            'commit': int,
            'compress-force': settings.COMPRESSION_TYPES,
            'degraded': None,
            'discard': None,
            'fatal_errors': None,
            'inode_cache': None,
            'max_inline': int,
            'metadata_ratio': int,
            'noacl': None,
            'noatime': None,
            'nodatacow': None,
            'nodatasum': None,
            'nospace_cache': None,
            'nossd': None,
            'ro': None,
            'rw': None,
            'skip_balance': None,
            'space_cache': None,
            'ssd': None,
            'ssd_spread': None,
            'thread_pool': int,
            '': None,
        }
        o_fields = mnt_options.split(',')
        for o in o_fields:
            v = None
            if (re.search('=', o) is not None):
                o, v = o.split('=')
            if (o not in allowed_options):
                e_msg = ('mount option(%s) not allowed. Make sure there are '
                         'no whitespaces in the input. Allowed options: %s' %
                         (o, allowed_options.keys()))
                handle_exception(Exception(e_msg), request)
            if ((o == 'compress-force' and
                 v not in allowed_options['compress-force'])):
                e_msg = ('compress-force is only allowed with {}'
                         .format(settings.COMPRESSION_TYPES))
                handle_exception(Exception(e_msg), request)
            # changed conditional from "if (type(allowed_options[o]) is int):"
            if (allowed_options[o] is int):
                try:
                    int(v)
                except:
                    e_msg = ('Value for mount option(%s) must be an integer' %
                             (o))
                    handle_exception(Exception(e_msg), request)
        return mnt_options

    @classmethod
    def _remount(cls, request, pool):
        compression = cls._validate_compression(request)
        mnt_options = cls._validate_mnt_options(request)
        if ((compression == pool.compression and
             mnt_options == pool.mnt_options)):
            return Response()

        with transaction.atomic():
            pool.compression = compression
            pool.mnt_options = mnt_options
            pool.save()

        if (re.search('noatime', mnt_options) is None):
            mnt_options = ('%s,relatime,atime' % mnt_options)

        if (re.search('compress-force', mnt_options) is None):
            mnt_options = ('%s,compress=%s' % (mnt_options, compression))

        with open('/proc/mounts') as mfo:
            mount_map = {}
            for l in mfo.readlines():
                share_name = None
                if (re.search(
                        '%s|%s' % (settings.NFS_EXPORT_ROOT, settings.MNT_PT),
                        l) is not None):
                    share_name = l.split()[1].split('/')[2]
                elif (re.search(settings.SFTP_MNT_ROOT, l) is not None):
                    share_name = l.split()[1].split('/')[3]
                else:
                    continue
                if (share_name not in mount_map):
                    mount_map[share_name] = [l.split()[1], ]
                else:
                    mount_map[share_name].append(l.split()[1])
        failed_remounts = []
        try:
            pool_mnt = '/mnt2/%s' % pool.name
            remount(pool_mnt, mnt_options)
        except:
            failed_remounts.append(pool_mnt)
        for share in mount_map.keys():
            if (Share.objects.filter(pool=pool, name=share).exists()):
                for m in mount_map[share]:
                    try:
                        remount(m, mnt_options)
                    except Exception as e:
                        logger.exception(e)
                        failed_remounts.append(m)
        if (len(failed_remounts) > 0):
            e_msg = ('Failed to remount the following mounts.\n %s\n '
                     'Try again or do the following as root(may cause '
                     'downtime):\n 1. systemctl stop rockstor\n'
                     '2. unmount manually\n3. systemctl start rockstor\n.' %
                     failed_remounts)
            handle_exception(Exception(e_msg), request)
        return Response(PoolInfoSerializer(pool).data)

    def _balance_start(self, pool, force=False, convert=None):
        mnt_pt = mount_root(pool)
        start_balance.async(mnt_pt, force=force, convert=convert)
        tid = 0
        count = 0
        while (tid == 0 and count < 25):
            for t in Task.objects.all():
                if (pickle.loads(t.args)[0] == mnt_pt):
                    tid = t.uuid
            time.sleep(0.2)
            count += 1
        logger.debug('balance tid = %s' % tid)
        return tid


class PoolListView(PoolMixin, rfc.GenericView):
    def get_queryset(self, *args, **kwargs):
        sort_col = self.request.query_params.get('sortby', None)
        if (sort_col is not None and sort_col == 'usage'):
            reverse = self.request.query_params.get('reverse', 'no')
            if (reverse == 'yes'):
                reverse = True
            else:
                reverse = False
            return sorted(Pool.objects.all(), key=lambda u: u.cur_usage(),
                          reverse=reverse)
        return Pool.objects.all()

    @transaction.atomic
    def post(self, request):
        """
        input is a list of disks, raid_level and name of the pool.
        """
        with self._handle_exception(request):
            disks = [self._validate_disk(d, request) for d in
                     request.data.get('disks')]
            pname = request.data['pname']
            if (re.match('%s$' % settings.POOL_REGEX, pname) is None):
                e_msg = ('Invalid characters in Pool name. Following '
                         'characters are allowed: letter(a-z or A-Z), '
                         'digit(0-9), '
                         'hyphen(-), underscore(_) or a period(.).')
                handle_exception(Exception(e_msg), request)

            if (len(pname) > 255):
                e_msg = ('Pool name must be less than 255 characters')
                handle_exception(Exception(e_msg), request)

            if (Pool.objects.filter(name=pname).exists()):
                e_msg = ('Pool(%s) already exists. Choose a different name'
                         % pname)
                handle_exception(Exception(e_msg), request)

            if (Share.objects.filter(name=pname).exists()):
                e_msg = ('A Share with this name(%s) exists. Pool and Share '
                         'names '
                         'must be distinct. Choose a different name' % pname)
                handle_exception(Exception(e_msg), request)

            for d in disks:
                if (d.btrfs_uuid is not None):
                    e_msg = ('Another BTRFS filesystem exists on this '
                             'disk(%s). Erase the disk and try again.'
                             % d.name)
                    handle_exception(Exception(e_msg), request)

            raid_level = request.data['raid_level']
            if (raid_level not in self.RAID_LEVELS):
                e_msg = ('Unsupported raid level. use one of: {}'.format(
                    self.RAID_LEVELS))
                handle_exception(Exception(e_msg), request)
            # consolidated raid0 & raid 1 disk check
            if (raid_level in self.RAID_LEVELS[1:3] and len(disks) <= 1):
                e_msg = ('At least two disks are required for the raid level: '
                         '%s' % raid_level)
                handle_exception(Exception(e_msg), request)
            if (raid_level == self.RAID_LEVELS[3]):
                if (len(disks) < 4):
                    e_msg = ('A minimum of Four drives are required for the '
                             'raid level: %s' % raid_level)
                    handle_exception(Exception(e_msg), request)
            if (raid_level == self.RAID_LEVELS[4] and len(disks) < 2):
                e_msg = ('Two or more disks are required for the raid '
                         'level: %s' % raid_level)
                handle_exception(Exception(e_msg), request)
            if (raid_level == self.RAID_LEVELS[5] and len(disks) < 3):
                e_msg = ('Three or more disks are required for the raid '
                         'level: %s' % raid_level)
                handle_exception(Exception(e_msg), request)

            compression = self._validate_compression(request)
            mnt_options = self._validate_mnt_options(request)
            dnames = self._role_filter_disk_names(disks, request)
            p = Pool(name=pname, raid=raid_level, compression=compression,
                     mnt_options=mnt_options)
            p.save()
            p.disk_set.add(*disks)
            # added for loop to save disks appears p.disk_set.add(*disks) was
            # not saving disks in test environment
            for d in disks:
                d.pool = p
                d.save()
            add_pool(p, dnames)
            p.size = p.usage_bound()
            p.uuid = btrfs_uuid(dnames[0])
            p.save()
            # Now we ensure udev info is updated via system wide trigger
            # as per pool resize add, only here it is for a new pool.
            trigger_udev_update()
            return Response(PoolInfoSerializer(p).data)


class PoolDetailView(PoolMixin, rfc.GenericView):
    def get(self, *args, **kwargs):
        try:
            pool = Pool.objects.get(id=self.kwargs['pid'])
            serialized_data = PoolInfoSerializer(pool)
            return Response(serialized_data.data)
        except Pool.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

    @transaction.atomic
    def put(self, request, pid, command):
        """
        resize a pool.
        @pname: pool's name
        @command: 'add' - add a list of disks and hence expand the pool
                  'remove' - remove a list of disks and hence shrink the pool
        """
        with self._handle_exception(request):
            try:
                pool = Pool.objects.get(id=pid)
            except:
                e_msg = ('Pool(%d) does not exist.' % pid)
                handle_exception(Exception(e_msg), request)

            if (pool.role == 'root'):
                e_msg = ('Edit operations are not allowed on this Pool(%d) '
                         'as it contains the operating system.' % pid)
                handle_exception(Exception(e_msg), request)

            if (command == 'remount'):
                return self._remount(request, pool)

            disks = [self._validate_disk(d, request) for d in
                     request.data.get('disks', [])]
            num_new_disks = len(disks)
            dnames = self._role_filter_disk_names(disks, request)
            new_raid = request.data.get('raid_level', pool.raid)
            num_total_disks = (Disk.objects.filter(pool=pool).count() +
                               num_new_disks)
            if (command == 'add'):
                for d in disks:
                    if (d.pool is not None):
                        e_msg = ('Disk(%s) cannot be added to this Pool(%s) '
                                 'because it belongs to another pool(%s)' %
                                 (d.name, pool.name, d.pool.name))
                        handle_exception(Exception(e_msg), request)
                    if (d.btrfs_uuid is not None):
                        e_msg = ('Disk(%s) has a BTRFS filesystem from the '
                                 'past. If you really like to add it, wipe it '
                                 'from the Storage -> Disks screen of the '
                                 'web-ui' % d.name)
                        handle_exception(Exception(e_msg), request)

                if (pool.raid != 'single' and new_raid == 'single'):
                    e_msg = ('Pool migration from %s to %s is not supported.'
                             % (pool.raid, new_raid))
                    handle_exception(Exception(e_msg), request)

                if (new_raid == 'raid10' and num_total_disks < 4):
                    e_msg = ('A minimum of Four drives are required for the '
                             'raid level: raid10')
                    handle_exception(Exception(e_msg), request)

                if (new_raid == 'raid6' and num_total_disks < 3):
                    e_msg = ('A minimum of Three drives are required for the '
                             'raid level: raid6')
                    handle_exception(Exception(e_msg), request)

                if (new_raid == 'raid5' and num_total_disks < 2):
                    e_msg = ('A minimum of Two drives are required for the '
                             'raid level: raid5')
                    handle_exception(Exception(e_msg), request)

                if (PoolBalance.objects.filter(
                        pool=pool,
                        status__regex=r'(started|running|cancelling|pausing|paused)').exists()):  # noqa E501
                    e_msg = ('A Balance process is already running or paused '
                             'for this pool(%s). Resize is not supported '
                             'during a balance process.' % pool.name)
                    handle_exception(Exception(e_msg), request)

                resize_pool(pool, dnames)
                tid = self._balance_start(pool, convert=new_raid)
                ps = PoolBalance(pool=pool, tid=tid)
                ps.save()

                pool.raid = new_raid
                for d_o in disks:
                    d_o.pool = pool
                    d_o.save()
                # Now we ensure udev info is updated via system wide trigger
                trigger_udev_update()
            elif (command == 'remove'):
                if (new_raid != pool.raid):
                    e_msg = ('Raid configuration cannot be changed while '
                             'removing disks')
                    handle_exception(Exception(e_msg), request)
                for d in disks:
                    if (d.pool is None or d.pool != pool):
                        e_msg = ('Disk(%s) cannot be removed because it does '
                                 'not belong to this Pool(%s)' %
                                 (d.name, pool.name))
                        handle_exception(Exception(e_msg), request)
                remaining_disks = (Disk.objects.filter(pool=pool).count() -
                                   num_new_disks)
                if (pool.raid == 'raid0'):
                    e_msg = ('Disks cannot be removed from a pool with this '
                             'raid(%s) configuration' % pool.raid)
                    handle_exception(Exception(e_msg), request)

                if (pool.raid == 'raid1' and remaining_disks < 2):
                    e_msg = ('Disks cannot be removed from this pool '
                             'because its raid configuration(raid1) '
                             'requires a minimum of 2 disks')
                    handle_exception(Exception(e_msg), request)

                if (pool.raid == 'raid10' and remaining_disks < 4):
                    e_msg = ('Disks cannot be removed from this pool '
                             'because its raid configuration(raid10) '
                             'requires a minimum of 4 disks')
                    handle_exception(Exception(e_msg), request)

                if (pool.raid == 'raid5' and remaining_disks < 2):
                    e_msg = ('Disks cannot be removed from this pool because '
                             'its raid configuration(raid5) requires a '
                             'minimum of 2 disks')
                    handle_exception(Exception(e_msg), request)

                if (pool.raid == 'raid6' and remaining_disks < 3):
                    e_msg = ('Disks cannot be removed from this pool because '
                             'its raid configuration(raid6) requires a '
                             'minimum of 3 disks')
                    handle_exception(Exception(e_msg), request)

                usage = pool_usage('/%s/%s' % (settings.MNT_PT, pool.name))
                size_cut = 0
                for d in disks:
                    size_cut += d.size
                if size_cut >= (pool.size - usage):
                    e_msg = ('Removing these(%s) disks may shrink the pool by '
                             '%dKB, which is greater than available free space'
                             ' %dKB. This is not supported.' %
                             (dnames, size_cut, usage))
                    handle_exception(Exception(e_msg), request)

                resize_pool(pool, dnames, add=False)
                tid = self._balance_start(pool)
                ps = PoolBalance(pool=pool, tid=tid)
                ps.save()

                for d in disks:
                    d.pool = None
                    d.save()

            else:
                e_msg = ('command(%s) is not supported.' % command)
                handle_exception(Exception(e_msg), request)
            pool.size = pool.usage_bound()
            pool.save()
            return Response(PoolInfoSerializer(pool).data)

    @transaction.atomic
    def delete(self, request, pid, command=''):
        force = True if (command == 'force') else False
        with self._handle_exception(request):
            try:
                pool = Pool.objects.get(id=pid)
            except:
                e_msg = ('Pool(%d) does not exist.' % pid)
                handle_exception(Exception(e_msg), request)

            if (pool.role == 'root'):
                e_msg = ('Deletion of Pool(%d) is not allowed as it contains '
                         'the operating system.' % pid)
                handle_exception(Exception(e_msg), request)

            if (Share.objects.filter(pool=pool).exists()):
                if not force:
                    e_msg = ('Pool(%d) is not empty. Delete is not allowed '
                             'until '
                             'all shares in the pool are deleted' % (pid))
                    handle_exception(Exception(e_msg), request)
                for so in Share.objects.filter(pool=pool):
                    remove_share(so.pool, so.subvol_name, so.pqgroup,
                                 force=force)
            pool_path = ('%s%s' % (settings.MNT_PT, pool.name))
            umount_root(pool_path)
            pool.delete()
            try:
                # TODO: this call fails as the inheritance of disks was removed
                # We need another method to invoke this as self no good now.
                self._update_disk_state()
            except Exception as e:
                logger.error('Exception while updating disk state: %s'
                             % e.__str__())
            return Response()


@api_view()
def get_usage_bound(request):
    """Simple view to relay the computed usage bound to the front end."""
    disk_sizes = [int(size) for size in
                  request.query_params.getlist('disk_sizes[]')]
    raid_level = request.query_params.get('raid_level', 'single')
    return Response(usage_bound(disk_sizes, len(disk_sizes), raid_level))
