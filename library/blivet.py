#!/usr/bin/python

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCUMENTATION = '''
---
module: blivet

short_description: Module for management of linux block device stacks

version_added: "2.5"

description:
    - "Module ___"

options:
    pools:
        description:
            - list of dicts describing pools
    volumes:
        description:
            - list of dicts describing volumes

author:
    - David Lehman (dlehman@redhat.com)
'''

EXAMPLES = '''

- name: Manage devices
  blivet:
    pools:
        - name: "{{ app_pool }}"
          disks: ["sdd", "sde"]
          volumes:
            - name: shared
              size: "10 GiB"
              mount_point: /opt/{{ app_pool }}/shared
            - name: web
              size: 8g
              mount_point: /opt/{{ app_pool }}/web
    volumes:
        - name: whole_disk1
          disks: ['sdc']
          mount_point: /whole_disk1
          fs_type: ext4
          mount_options: journal_checksum,async,noexec
'''

RETURN = '''
actions:
    description: list of strings describing actions taken
    type: list of str
leaves:
    description: list of paths to leaf devices
    type: list of str
mounts:
    description: list of dicts describing mounts to set up
    type: list of dict
removed_mounts:
    description: list of mount points of removed mounts
    type: list of str
'''

from blivet import Blivet
from blivet.callbacks import callbacks
from blivet.flags import flags as blivet_flags
from blivet.formats import get_format
from blivet.partitioning import do_partitioning
from blivet.size import Size
from blivet.util import set_up_logging

from ansible.module_utils.basic import AnsibleModule
#from ansible.module_utils.size import Size

blivet_flags.debug = True
set_up_logging()
import logging
log = logging.getLogger("blivet.ansible")


use_partitions = None  # create partitions on pool backing device disks?
disklabel_type = None  # user-specified disklabel type


class BlivetVolume:
    def __init__(self, blivet_obj, volume, bpool=None):
        self._blivet = blivet_obj
        self._volume = volume
        self._blivet_pool = bpool
        self._device = None

    @property
    def ultimately_present(self):
        return (self._volume['state'] == 'present' and
                (self._blivet_pool is None or self._blivet_pool.ultimately_present))

    def _type_check(self):  # pylint: disable=no-self-use
        return True

    def _get_device_id(self):
        return self._volume['name']

    def _look_up_device(self):
        device = self._blivet.devicetree.resolve_device(self._get_device_id())
        if device is None:
            return

        self._device = device

        # check that the type is correct, raising an exception if there is a name conflict
        if not self._type_check():
            self._device = None
            return  # TODO: see if we can create this device w/ the specified name

    def _get_format(self):
        return get_format(self._volume['fs_type'],
                          mountpoint=self._volume.get('mount_point'),
                          label=self._volume['fs_label'],
                          options=self._volume['fs_create_options'])

    def _create(self):
        pass

    def _destroy(self):
        if self._device is None:
            return

        # save device identifiers for use by the role
        self._volume['_device'] = self._device.path
        self._volume['_mount_id'] = self._device.fstab_spec

        # schedule removal of this device and any descendant devices
        self._blivet.devicetree.recursive_remove(self._device)

    def _resize(self):
        size = Size(self._volume['size'])
        if size and self._device.resizable and self._device.size != size:
            if self._device.format.resizable:
                self._device.format.update_size_info()

            try:
                self._blivet.resize_device(self._device, size)
            except ValueError as e:
                raise RuntimeError("device '%s' is not resizable (%s -> %s): %s"
                                   % (self._device.name, self._device.size, size, str(e)))

    def _reformat(self):
        fmt = self._get_format()
        if self._device.format.type == fmt.type:
            return

        if self._device.format.status:
            self._device.format.teardown()
        self._blivet.format_device(self._device, fmt)

    def manage(self):
        # look up the device
        self._look_up_device()

        # schedule destroy if appropriate
        if not self.ultimately_present:
            self._destroy()
            return

        # schedule create if appropriate
        self._create()

        # at this point we should have a blivet.devices.StorageDevice instance
        if self._device is None:
            raise RuntimeError("failed to look up or create device '%s'" % self._volume['name'])

        # schedule reformat if appropriate
        if self._device.exists:
            self._reformat()

        # schedule resize if appropriate
        if self._device.exists and self._volume['size']:
            self._resize()

        # save device identifiers for use by the role
        self._volume['_device'] = self._device.path
        self._volume['_mount_id'] = self._device.fstab_spec


class BlivetDiskVolume(BlivetVolume):
    def _get_device_id(self):
        return self._volume['disks'][0]

    def _type_check(self):
        return self._device.is_disk


class BlivetPartitionVolume(BlivetVolume):
    def _type_check(self):
        return self._device.type == 'partition'

    def _get_device_id(self):
        return self._blivet_pool._disks[0].name + '1'

    def _create(self):
        if self._device:
            return

        if self._blivet_pool:
            parent = self._blivet_pool._device
        else:
            parent = self._blivet.devicetree.resolve_device(self._volume['pool'])

        size = Size("256 MiB")
        device = self._blivet.new_partition(parents=[parent], size=size, grow=True, fmt=self._get_format())
        self._blivet.create_device(device)
        do_partitioning(self._blivet)
        self._device = device


class BlivetLVMVolume(BlivetVolume):
    def _get_device_id(self):
        return "%s-%s" % (self._blivet_pool._device.name, self._volume['name'])

    def _create(self):
        if self._device:
            return

        parent = self._blivet_pool._device
        size = Size(self._volume['size'])
        fmt = self._get_format()
        try:
            device = self._blivet.new_lv(name=self._volume['name'],
                                         parents=[parent], size=size, fmt=fmt)
        except Exception as e:
            raise RuntimeError("failed to create lv '%s': %s" % (self._volume['name'], str(e)))

        self._blivet.create_device(device)
        self._device = device


_BLIVET_VOLUME_TYPES = {
    "disk": BlivetDiskVolume,
    "lvm": BlivetLVMVolume,
    "partition": BlivetPartitionVolume
}


def _get_blivet_volume(blivet_obj, volume, bpool=None):
    volume_type = volume.get('type', bpool._pool['type'] if bpool else None)
    if volume_type not in _BLIVET_VOLUME_TYPES:
        raise RuntimeError("Volume '%s' has unknown type '%s'" % (volume['name'], volume_type))

    return _BLIVET_VOLUME_TYPES[volume_type](blivet_obj, volume, bpool=bpool)


class BlivetPool:
    def __init__(self, blivet_obj, pool):
        self._blivet = blivet_obj
        self._pool = pool
        self._device = None
        self._disks = list()
        self._blivet_volumes = list()

    @property
    def ultimately_present(self):
        return self._pool['state'] == 'present'

    def _create(self):
        pass

    def _destroy(self):
        if self._device is None:
            return

        ancestors = self._device.ancestors  # ascending distance ordering
        log.debug("%s", [a.name for a in ancestors])
        self._blivet.devicetree.recursive_remove(self._device)
        ancestors.remove(self._device)
        leaves = [a for a in ancestors if a.isleaf]
        while leaves:
            for ancestor in leaves:
                log.info("scheduling destruction of %s", ancestor.name)
                if ancestor.is_disk:
                    self._blivet.devicetree.recursive_remove(ancestor)
                else:
                    self._blivet.destroy_device(ancestor)

                ancestors.remove(ancestor)

            leaves = [a for a in ancestors if a.isleaf]

    def _type_check(self):  # pylint: disable=no-self-use
        return True

    def _look_up_disks(self):
        disks = list()
        for spec in self._pool['disks']:
            device = self._blivet.devicetree.resolve_device(spec)
            if device is not None:
                disks.append(device)

        self._disks = disks

    def _look_up_device(self):
        device = self._blivet.devicetree.resolve_device(self._pool['name'])
        if device is None:
            return

        self._device = device

        # check that the type is correct, raising an exception if there is a name conflict
        if not self._type_check():
            self._device = None
            return  # TODO: see if we can create this device w/ the specified name

    def _create_members(self):
        members = list()
        for disk in self._disks:
            if not disk.isleaf:
                self._blivet.devicetree.recursive_remove(disk)

            if use_partitions:
                label = get_format("disklabel", device=disk.path)
                self._blivet.format_device(disk, label)
                member = self._blivet.new_partition(parents=[disk], size=Size("256MiB"), grow=True)
                self._blivet.create_device(member)
            else:
                member = disk

            self._blivet.format_device(member, get_format("lvmpv"))
            members.append(member)

        if use_partitions:
            do_partitioning(self._blivet)

        return members

    def _get_volumes(self):
        for volume in self._pool['volumes']:
            bvolume = _get_blivet_volume(self._blivet, volume, self)
            self._blivet_volumes.append(bvolume)

    def _manage_volumes(self):
        self._get_volumes()
        for bvolume in self._blivet_volumes:
            bvolume.manage()

    def manage(self):
        # look up the device
        self._look_up_disks()
        self._look_up_device()

        # schedule destroy if appropriate, including member type change
        if not self.ultimately_present:  # TODO: member type changes
            self._destroy()
            return

        # schedule create if appropriate
        self._create()
        self._manage_volumes()


class BlivetPartitionPool(BlivetPool):
    def _type_check(self):
        return self._device.partitionable

    def _look_up_device(self):
        self._device = self._disks[0]

    def _create(self):
        if self._device.format.type != "disklabel" or \
           self._device.format.label_type != disklabel_type:
            self._blivet.devicetree.recursive_remove(self._device, remove_device=False)

            label = get_format("disklabel", device=self._device.path, label_type=disklabel_type)
            self._blivet.format_device(self._device, label)


class BlivetLVMPool(BlivetPool):
    def _type_check(self):
        return self._device.type == "lvmvg"

    def _create(self):
        if self._device:
            return

        members = self._create_members()
        pool_device = self._blivet.new_vg(name=self._pool['name'], parents=members)
        self._blivet.create_device(pool_device)
        self._device = pool_device


_BLIVET_POOL_TYPES = {
    "disk": BlivetPartitionPool,
    "lvm": BlivetLVMPool
}


def _get_blivet_pool(blivet_obj, pool):
    if pool['type'] not in _BLIVET_POOL_TYPES:
        raise RuntimeError("Pool '%s' has unknown type '%s'" % (pool['name'], pool['type']))

    return _BLIVET_POOL_TYPES[pool['type']](blivet_obj, pool)


def manage_volume(b, volume):
    bvolume = _get_blivet_volume(b, volume)
    bvolume.manage()
    volume['_device'] = bvolume._volume.get('_device', '')
    volume['_mount_id'] = bvolume._volume.get('_mount_id', '')


def manage_pool(b, pool):
    bpool = _get_blivet_pool(b, pool)
    bpool.manage()
    for (volume, bvolume) in zip(pool['volumes'], bpool._blivet_volumes):
        volume['_device'] = bvolume._volume.get('_device', '')
        volume['_mount_id'] = bvolume._volume.get('_mount_id', '')


def get_fstab_mounts(b):
    mounts = {}
    for line in open('/etc/fstab').readlines():
        if line.lstrip().startswith("#"):
            continue

        fields = line.split()
        if len(fields) < 6:
            continue

        device_id = fields[0]
        mount_point = fields[1]
        device = b.devicetree.resolve_device(device_id)
        if device is not None:
            mounts[device.name] = mount_point

    return mounts


def run_module():
    # available arguments/parameters that a user can pass
    module_args = dict(
        pools=dict(type='list'),
        volumes=dict(type='list'),
        disklabel_type=dict(type='str', required=False, default=None),
        use_partitions=dict(type='bool', required=False, default=True))

    # seed the result dict in the object
    result = dict(
        changed=False,
        actions=list(),
        leaves=list(),
        mounts=list(),
        pools=list(),
        volumes=list(),
    )

    module = AnsibleModule(argument_spec=module_args,
                           supports_check_mode=True)

    if not module.params['pools'] and not module.params['volumes']:
        module.exit_json(**result)

    global disklabel_type
    disklabel_type = module.params['disklabel_type']

    global use_partitions
    use_partitions = module.params['use_partitions']

    b = Blivet()
    b.reset()
    actions = list()
    initial_mounts = get_fstab_mounts(b)

    def record_action(action):
        if action.is_format and action.format.type is None:
            return

        actions.append(action)

    def action_string(action):
        action_desc = '{act}'
        if action.is_format:
            action_desc += ' {fmt} on'
        action_desc += ' {dev}'
        return action_desc.format(act=action.type_desc_str,
                                  fmt=action.format.type,
                                  dev=action.device.path)

    for pool in module.params['pools']:
        manage_pool(b, pool)

    for volume in module.params['volumes']:
        manage_volume(b, volume)

    scheduled = b.devicetree.actions.find()
    for action in scheduled:
        if action.is_destroy and action.is_format and action.format.exists:
            action.format.teardown()

    if scheduled:
        ## execute the scheduled actions, committing changes to disk
        callbacks.action_executed.add(record_action)
        b.devicetree.actions.process(devices=b.devicetree.devices, dry_run=module.check_mode)
        result['changed'] = True
        result['actions'] = [action_string(a) for a in actions]

        ## build a list of mounts to remove
        for action in actions:
            if action.is_destroy and action.is_format and action.format.type is not None:
                mount = initial_mounts.get(action.device.name)
                if mount is not None:
                    result['mounts'].append({"path": mount, 'state': 'absent'})

    mount_vols = list()
    for pool in module.params['pools']:
        for volume in pool['volumes']:
            if pool['state'] == 'present' and volume['state'] == 'present':
                mount = initial_mounts.get(volume['_device'].split('/')[-1])
                if volume['mount_point']:
                    mount_vols.append(volume.copy())

                # handle removal of existing mounts of this volume
                if mount and mount != volume['mount_point']:
                    result['mounts'].append({"path": mount, 'state': 'absent'})

    for volume in module.params['volumes']:
        if volume['state'] == 'present':
            mount = initial_mounts.get(volume['_device'].split('/')[-1])
            if volume['mount_point']:
                mount_vols.append(volume)

            # handle removal of existing mounts of this volume
            if mount and mount != volume['mount_point']:
                result['mounts'].append({"path": mount, 'state': 'absent'})

    for volume in mount_vols:
        result['mounts'].append({'src': volume['_device'],
                                 'path': volume['mount_point'],
                                 'fstype': volume['fs_type'],
                                 'opts': volume['mount_options'],
                                 'dump': volume['mount_check'],
                                 'passno': volume['mount_passno'],
                                 'state': 'mounted'})

    result['leaves'] = [d.path for d in b.devicetree.leaves]
    result['pools'] = module.params['pools']
    result['volumes'] = module.params['volumes']

    # success - return result
    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
