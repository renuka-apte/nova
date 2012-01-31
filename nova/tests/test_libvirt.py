# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
#    Copyright 2010 OpenStack LLC
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import errno
import eventlet
import mox
import os
import re
import shutil
import tempfile

from lxml import etree
from xml.dom import minidom

from nova.api.ec2 import cloud
from nova.compute import instance_types
from nova.compute import power_state
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova import context
from nova import db
from nova import exception
from nova import flags
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova import test
from nova.tests import fake_libvirt_utils
from nova.tests import fake_network
import nova.tests.image.fake
from nova import utils
from nova.virt import driver
from nova.virt import firewall as base_firewall
from nova.virt import images
from nova.virt.libvirt import config
from nova.virt.libvirt import driver as libvirt_driver
from nova.virt.libvirt import firewall
from nova.virt.libvirt import imagebackend
from nova.virt.libvirt import utils as libvirt_utils
from nova.virt.libvirt import volume
from nova.volume import driver as volume_driver


try:
    import libvirt
except ImportError:
    import nova.tests.fakelibvirt as libvirt
libvirt_driver.libvirt = libvirt


FLAGS = flags.FLAGS
LOG = logging.getLogger(__name__)

_fake_network_info = fake_network.fake_get_instance_nw_info
_fake_stub_out_get_nw_info = fake_network.stub_out_nw_api_get_instance_nw_info
_ipv4_like = fake_network.ipv4_like


def _concurrency(wait, done, target):
    wait.wait()
    done.send()


class FakeVirDomainSnapshot(object):

    def __init__(self, dom=None):
        self.dom = dom

    def delete(self, flags):
        pass


class FakeVirtDomain(object):

    def __init__(self, fake_xml=None):
        if fake_xml:
            self._fake_dom_xml = fake_xml
        else:
            self._fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                    </devices>
                </domain>
            """

    def name(self):
        return "fake-domain %s" % self

    def info(self):
        return [power_state.RUNNING, None, None, None, None]

    def create(self):
        pass

    def managedSave(self, *args):
        pass

    def createWithFlags(self, launch_flags):
        pass

    def XMLDesc(self, *args):
        return self._fake_dom_xml


class LibvirtVolumeTestCase(test.TestCase):

    def setUp(self):
        super(LibvirtVolumeTestCase, self).setUp()
        self.executes = []

        def fake_execute(*cmd, **kwargs):
            self.executes.append(cmd)
            return None, None

        self.stubs.Set(utils, 'execute', fake_execute)

        class FakeLibvirtDriver(object):
            def __init__(self, hyperv="QEMU"):
                self.hyperv = hyperv

            def get_hypervisor_type(self):
                return self.hyperv

            def get_all_block_devices(self):
                return []

        self.fake_conn = FakeLibvirtDriver()
        self.connr = {
            'ip': '127.0.0.1',
            'initiator': 'fake_initiator',
            'host': 'fake_host'
        }

    def test_libvirt_iscsi_driver(self):
        # NOTE(vish) exists is to make driver assume connecting worked
        self.stubs.Set(os.path, 'exists', lambda x: True)
        vol_driver = volume_driver.ISCSIDriver()
        libvirt_driver = volume.LibvirtISCSIVolumeDriver(self.fake_conn)
        location = '10.0.2.15:3260'
        name = 'volume-00000001'
        iqn = 'iqn.2010-10.org.openstack:%s' % name
        vol = {'id': 1,
               'name': name,
               'provider_auth': None,
               'provider_location': '%s,fake %s' % (location, iqn)}
        connection_info = vol_driver.initialize_connection(vol, self.connr)
        mount_device = "vde"
        conf = libvirt_driver.connect_volume(connection_info, mount_device)
        tree = conf.format_dom()
        dev_str = '/dev/disk/by-path/ip-%s-iscsi-%s-lun-1' % (location, iqn)
        self.assertEqual(tree.get('type'), 'block')
        self.assertEqual(tree.find('./source').get('dev'), dev_str)
        libvirt_driver.disconnect_volume(connection_info, mount_device)
        connection_info = vol_driver.terminate_connection(vol, self.connr)
        expected_commands = [('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location),
                             ('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location, '--login'),
                             ('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location, '--op', 'update',
                              '-n', 'node.startup', '-v', 'automatic'),
                             ('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location, '--op', 'update',
                              '-n', 'node.startup', '-v', 'manual'),
                             ('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location, '--logout'),
                             ('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location, '--op', 'delete')]
        self.assertEqual(self.executes, expected_commands)

    def test_libvirt_iscsi_driver_still_in_use(self):
        # NOTE(vish) exists is to make driver assume connecting worked
        self.stubs.Set(os.path, 'exists', lambda x: True)
        vol_driver = volume_driver.ISCSIDriver()
        libvirt_driver = volume.LibvirtISCSIVolumeDriver(self.fake_conn)
        location = '10.0.2.15:3260'
        name = 'volume-00000001'
        iqn = 'iqn.2010-10.org.openstack:%s' % name
        devs = ['/dev/disk/by-path/ip-%s-iscsi-%s-lun-1' % (location, iqn)]
        self.stubs.Set(self.fake_conn, 'get_all_block_devices', lambda: devs)
        vol = {'id': 1,
               'name': name,
               'provider_auth': None,
               'provider_location': '%s,fake %s' % (location, iqn)}
        connection_info = vol_driver.initialize_connection(vol, self.connr)
        mount_device = "vde"
        conf = libvirt_driver.connect_volume(connection_info, mount_device)
        tree = conf.format_dom()
        dev_str = '/dev/disk/by-path/ip-%s-iscsi-%s-lun-1' % (location, iqn)
        self.assertEqual(tree.get('type'), 'block')
        self.assertEqual(tree.find('./source').get('dev'), dev_str)
        libvirt_driver.disconnect_volume(connection_info, mount_device)
        connection_info = vol_driver.terminate_connection(vol, self.connr)
        expected_commands = [('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location),
                             ('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location, '--login'),
                             ('iscsiadm', '-m', 'node', '-T', iqn,
                              '-p', location, '--op', 'update',
                              '-n', 'node.startup', '-v', 'automatic')]
        self.assertEqual(self.executes, expected_commands)

    def test_libvirt_sheepdog_driver(self):
        vol_driver = volume_driver.SheepdogDriver()
        libvirt_driver = volume.LibvirtNetVolumeDriver(self.fake_conn)
        name = 'volume-00000001'
        vol = {'id': 1, 'name': name}
        connection_info = vol_driver.initialize_connection(vol, self.connr)
        mount_device = "vde"
        conf = libvirt_driver.connect_volume(connection_info, mount_device)
        tree = conf.format_dom()
        self.assertEqual(tree.get('type'), 'network')
        self.assertEqual(tree.find('./source').get('protocol'), 'sheepdog')
        self.assertEqual(tree.find('./source').get('name'), name)
        libvirt_driver.disconnect_volume(connection_info, mount_device)
        connection_info = vol_driver.terminate_connection(vol, self.connr)

    def test_libvirt_rbd_driver(self):
        vol_driver = volume_driver.RBDDriver()
        libvirt_driver = volume.LibvirtNetVolumeDriver(self.fake_conn)
        name = 'volume-00000001'
        vol = {'id': 1, 'name': name}
        connection_info = vol_driver.initialize_connection(vol, self.connr)
        mount_device = "vde"
        conf = libvirt_driver.connect_volume(connection_info, mount_device)
        tree = conf.format_dom()
        self.assertEqual(tree.get('type'), 'network')
        self.assertEqual(tree.find('./source').get('protocol'), 'rbd')
        rbd_name = '%s/%s' % (FLAGS.rbd_pool, name)
        self.assertEqual(tree.find('./source').get('name'), rbd_name)
        self.assertEqual(tree.find('./source/auth'), None)
        libvirt_driver.disconnect_volume(connection_info, mount_device)
        connection_info = vol_driver.terminate_connection(vol, self.connr)

    def test_libvirt_rbd_driver_auth_enabled(self):
        vol_driver = volume_driver.RBDDriver()
        libvirt_driver = volume.LibvirtNetVolumeDriver(self.fake_conn)
        name = 'volume-00000001'
        vol = {'id': 1, 'name': name}
        connection_info = vol_driver.initialize_connection(vol, self.connr)
        uuid = '875a8070-d0b9-4949-8b31-104d125c9a64'
        user = 'foo'
        secret_type = 'ceph'
        connection_info['data']['auth_enabled'] = True
        connection_info['data']['auth_username'] = user
        connection_info['data']['secret_type'] = secret_type
        connection_info['data']['secret_uuid'] = uuid

        mount_device = "vde"
        conf = libvirt_driver.connect_volume(connection_info, mount_device)
        tree = conf.format_dom()
        self.assertEqual(tree.get('type'), 'network')
        self.assertEqual(tree.find('./source').get('protocol'), 'rbd')
        rbd_name = '%s/%s' % (FLAGS.rbd_pool, name)
        self.assertEqual(tree.find('./source').get('name'), rbd_name)
        self.assertEqual(tree.find('./auth').get('username'), user)
        self.assertEqual(tree.find('./auth/secret').get('type'), secret_type)
        self.assertEqual(tree.find('./auth/secret').get('uuid'), uuid)
        libvirt_driver.disconnect_volume(connection_info, mount_device)
        connection_info = vol_driver.terminate_connection(vol, self.connr)

    def test_libvirt_rbd_driver_auth_disabled(self):
        vol_driver = volume_driver.RBDDriver()
        libvirt_driver = volume.LibvirtNetVolumeDriver(self.fake_conn)
        name = 'volume-00000001'
        vol = {'id': 1, 'name': name}
        connection_info = vol_driver.initialize_connection(vol, self.connr)
        uuid = '875a8070-d0b9-4949-8b31-104d125c9a64'
        user = 'foo'
        secret_type = 'ceph'
        connection_info['data']['auth_enabled'] = False
        connection_info['data']['auth_username'] = user
        connection_info['data']['secret_type'] = secret_type
        connection_info['data']['secret_uuid'] = uuid

        mount_device = "vde"
        conf = libvirt_driver.connect_volume(connection_info, mount_device)
        tree = conf.format_dom()
        self.assertEqual(tree.get('type'), 'network')
        self.assertEqual(tree.find('./source').get('protocol'), 'rbd')
        rbd_name = '%s/%s' % (FLAGS.rbd_pool, name)
        self.assertEqual(tree.find('./source').get('name'), rbd_name)
        self.assertEqual(tree.find('./auth'), None)
        libvirt_driver.disconnect_volume(connection_info, mount_device)
        connection_info = vol_driver.terminate_connection(vol, self.connr)

    def test_libvirt_lxc_volume(self):
        self.stubs.Set(os.path, 'exists', lambda x: True)
        vol_driver = volume_driver.ISCSIDriver()
        libvirt_driver = volume.LibvirtISCSIVolumeDriver(self.fake_conn)
        location = '10.0.2.15:3260'
        name = 'volume-00000001'
        iqn = 'iqn.2010-10.org.openstack:%s' % name
        vol = {'id': 1,
               'name': name,
               'provider_auth': None,
               'provider_location': '%s,fake %s' % (location, iqn)}
        connection_info = vol_driver.initialize_connection(vol, self.connr)
        mount_device = "vde"
        conf = libvirt_driver.connect_volume(connection_info, mount_device)
        tree = conf.format_dom()
        dev_str = '/dev/disk/by-path/ip-%s-iscsi-%s-lun-1' % (location, iqn)
        self.assertEqual(tree.get('type'), 'block')
        self.assertEqual(tree.find('./source').get('dev'), dev_str)
        libvirt_driver.disconnect_volume(connection_info, mount_device)
        connection_info = vol_driver.terminate_connection(vol, self.connr)


class CacheConcurrencyTestCase(test.TestCase):
    def setUp(self):
        super(CacheConcurrencyTestCase, self).setUp()
        self.flags(instances_path='nova.compute.manager')

        def fake_exists(fname):
            basedir = os.path.join(FLAGS.instances_path, FLAGS.base_dir_name)
            if fname == basedir:
                return True
            return False

        def fake_execute(*args, **kwargs):
            pass

        def fake_extend(image, size):
            pass

        self.stubs.Set(os.path, 'exists', fake_exists)
        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(imagebackend.disk, 'extend', fake_extend)
        imagebackend.libvirt_utils = fake_libvirt_utils

    def tearDown(self):
        imagebackend.libvirt_utils = libvirt_utils
        super(CacheConcurrencyTestCase, self).tearDown()

    def test_same_fname_concurrency(self):
        """Ensures that the same fname cache runs at a sequentially"""
        backend = imagebackend.Backend(False)
        wait1 = eventlet.event.Event()
        done1 = eventlet.event.Event()
        eventlet.spawn(backend.image('instance', 'name').cache,
                _concurrency, 'fname', None, wait=wait1, done=done1)
        wait2 = eventlet.event.Event()
        done2 = eventlet.event.Event()
        eventlet.spawn(backend.image('instance', 'name').cache,
                _concurrency, 'fname', None, wait=wait2, done=done2)
        wait2.send()
        eventlet.sleep(0)
        try:
            self.assertFalse(done2.ready())
        finally:
            wait1.send()
        done1.wait()
        eventlet.sleep(0)
        self.assertTrue(done2.ready())

    def test_different_fname_concurrency(self):
        """Ensures that two different fname caches are concurrent"""
        backend = imagebackend.Backend(False)
        wait1 = eventlet.event.Event()
        done1 = eventlet.event.Event()
        eventlet.spawn(backend.image('instance', 'name').cache,
                _concurrency, 'fname2', None, wait=wait1, done=done1)
        wait2 = eventlet.event.Event()
        done2 = eventlet.event.Event()
        eventlet.spawn(backend.image('instance', 'name').cache,
                _concurrency, 'fname1', None, wait=wait2, done=done2)
        wait2.send()
        eventlet.sleep(0)
        try:
            self.assertTrue(done2.ready())
        finally:
            wait1.send()
            eventlet.sleep(0)


class FakeVolumeDriver(object):
    def __init__(self, *args, **kwargs):
        pass

    def attach_volume(self, *args):
        pass

    def detach_volume(self, *args):
        pass

    def get_xml(self, *args):
        return ""


class LibvirtConnTestCase(test.TestCase):

    def setUp(self):
        super(LibvirtConnTestCase, self).setUp()
        libvirt_driver._late_load_cheetah()
        self.flags(fake_call=True)
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.get_admin_context()
        self.flags(instances_path='')
        self.call_libvirt_dependant_setup = False
        libvirt_driver.libvirt_utils = fake_libvirt_utils

        def fake_extend(image, size):
            pass

        self.stubs.Set(libvirt_driver.disk, 'extend', fake_extend)

        nova.tests.image.fake.stub_out_image_service(self.stubs)

    def tearDown(self):
        libvirt_driver.libvirt_utils = libvirt_utils
        nova.tests.image.fake.FakeImageService_reset()
        super(LibvirtConnTestCase, self).tearDown()

    test_instance = {'memory_kb': '1024000',
                     'basepath': '/some/path',
                     'bridge_name': 'br100',
                     'vcpus': 2,
                     'project_id': 'fake',
                     'bridge': 'br101',
                     'image_ref': '155d900f-4e14-4e4c-a73d-069cbf4541e6',
                     'root_gb': 10,
                     'ephemeral_gb': 20,
                     'instance_type_id': '5'}  # m1.small

    def create_fake_libvirt_mock(self, **kwargs):
        """Defining mocks for LibvirtDriver(libvirt is not used)."""

        # A fake libvirt.virConnect
        class FakeLibvirtDriver(object):
            def defineXML(self, xml):
                return FakeVirtDomain()

        # Creating mocks
        volume_driver = 'iscsi=nova.tests.test_libvirt.FakeVolumeDriver'
        self.flags(libvirt_volume_drivers=[volume_driver])
        fake = FakeLibvirtDriver()
        # Customizing above fake if necessary
        for key, val in kwargs.items():
            fake.__setattr__(key, val)

        self.flags(libvirt_vif_driver="nova.tests.fake_network.FakeVIFDriver")

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn = fake

    def fake_lookup(self, instance_name):
        return FakeVirtDomain()

    def fake_execute(self, *args):
        open(args[-1], "a").close()

    def create_service(self, **kwargs):
        service_ref = {'host': kwargs.get('host', 'dummy'),
                       'binary': 'nova-compute',
                       'topic': 'compute',
                       'report_count': 0,
                       'availability_zone': 'zone'}

        return db.service_create(context.get_admin_context(), service_ref)

    def test_get_connector(self):
        initiator = 'fake.initiator.iqn'
        ip = 'fakeip'
        host = 'fakehost'
        self.flags(my_ip=ip)
        self.flags(host=host)

        conn = libvirt_driver.LibvirtDriver(True)
        expected = {
            'ip': ip,
            'initiator': initiator,
            'host': host
        }
        volume = {
            'id': 'fake'
        }
        result = conn.get_volume_connector(volume)
        self.assertDictMatch(expected, result)

    def test_get_guest_config(self):
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        cfg = conn.get_guest_config(instance_ref,
                                    _fake_network_info(self.stubs, 1),
                                    None, None)
        self.assertEquals(cfg.acpi, True)
        self.assertEquals(cfg.memory, 1024 * 1024 * 2)
        self.assertEquals(cfg.vcpus, 1)
        self.assertEquals(cfg.os_type, "hvm")
        self.assertEquals(cfg.os_boot_dev, "hd")
        self.assertEquals(cfg.os_root, None)
        self.assertEquals(len(cfg.devices), 7)
        self.assertEquals(type(cfg.devices[0]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          config.LibvirtConfigGuestInterface)
        self.assertEquals(type(cfg.devices[3]),
                          config.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[4]),
                          config.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[5]),
                          config.LibvirtConfigGuestInput)
        self.assertEquals(type(cfg.devices[6]),
                          config.LibvirtConfigGuestGraphics)

        self.assertEquals(type(cfg.clock),
                          config.LibvirtConfigGuestClock)
        self.assertEquals(cfg.clock.offset, "utc")
        self.assertEquals(len(cfg.clock.timers), 2)
        self.assertEquals(type(cfg.clock.timers[0]),
                          config.LibvirtConfigGuestTimer)
        self.assertEquals(type(cfg.clock.timers[1]),
                          config.LibvirtConfigGuestTimer)
        self.assertEquals(cfg.clock.timers[0].name, "pit")
        self.assertEquals(cfg.clock.timers[0].tickpolicy,
                          "delay")
        self.assertEquals(cfg.clock.timers[1].name, "rtc")
        self.assertEquals(cfg.clock.timers[1].tickpolicy,
                          "catchup")

    def test_get_guest_config_with_two_nics(self):
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        cfg = conn.get_guest_config(instance_ref,
                                    _fake_network_info(self.stubs, 2),
                                    None, None)
        self.assertEquals(cfg.acpi, True)
        self.assertEquals(cfg.memory, 1024 * 1024 * 2)
        self.assertEquals(cfg.vcpus, 1)
        self.assertEquals(cfg.os_type, "hvm")
        self.assertEquals(cfg.os_boot_dev, "hd")
        self.assertEquals(cfg.os_root, None)
        self.assertEquals(len(cfg.devices), 8)
        self.assertEquals(type(cfg.devices[0]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          config.LibvirtConfigGuestInterface)
        self.assertEquals(type(cfg.devices[3]),
                          config.LibvirtConfigGuestInterface)
        self.assertEquals(type(cfg.devices[4]),
                          config.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[5]),
                          config.LibvirtConfigGuestSerial)
        self.assertEquals(type(cfg.devices[6]),
                          config.LibvirtConfigGuestInput)
        self.assertEquals(type(cfg.devices[7]),
                          config.LibvirtConfigGuestGraphics)

    def test_get_guest_config_with_root_device_name(self):
        self.flags(libvirt_type='uml')
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        cfg = conn.get_guest_config(instance_ref, [], None, None,
                                    {'root_device_name': 'dev/vdb'})
        self.assertEquals(cfg.acpi, False)
        self.assertEquals(cfg.memory, 1024 * 1024 * 2)
        self.assertEquals(cfg.vcpus, 1)
        self.assertEquals(cfg.os_type, "uml")
        self.assertEquals(cfg.os_boot_dev, None)
        self.assertEquals(cfg.os_root, 'dev/vdb')
        self.assertEquals(len(cfg.devices), 3)
        self.assertEquals(type(cfg.devices[0]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[1]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(type(cfg.devices[2]),
                          config.LibvirtConfigGuestConsole)

    def test_get_guest_config_with_block_device(self):
        conn = libvirt_driver.LibvirtDriver(True)

        instance_ref = db.instance_create(self.context, self.test_instance)
        conn_info = {'driver_volume_type': 'fake'}
        info = {'block_device_mapping': [
                  {'connection_info': conn_info, 'mount_device': '/dev/vdc'},
                  {'connection_info': conn_info, 'mount_device': '/dev/vdd'}]}

        cfg = conn.get_guest_config(instance_ref, [], None, None, info)
        self.assertEquals(type(cfg.devices[2]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(cfg.devices[2].target_dev, 'vdc')
        self.assertEquals(type(cfg.devices[3]),
                          config.LibvirtConfigGuestDisk)
        self.assertEquals(cfg.devices[3].target_dev, 'vdd')

    def test_get_guest_cpu_config_none(self):
        self.flags(libvirt_cpu_mode="none")
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        conf = conn.get_guest_config(instance_ref,
                                    _fake_network_info(self.stubs, 1),
                                    None, None)
        self.assertEquals(conf.cpu, None)

    def test_get_guest_cpu_config_default_kvm(self):
        self.flags(libvirt_type="kvm",
                   libvirt_cpu_mode=None)

        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, None)
        self.assertEquals(type(conf.cpu),
                          config.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "host-model")
        self.assertEquals(conf.cpu.model, None)

    def test_get_guest_cpu_config_default_uml(self):
        self.flags(libvirt_type="uml",
                   libvirt_cpu_mode=None)

        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        conf = conn.get_guest_config(instance_ref,
                                    _fake_network_info(self.stubs, 1),
                                    None, None)
        self.assertEquals(conf.cpu, None)

    def test_get_guest_cpu_config_default_lxc(self):
        self.flags(libvirt_type="lxc",
                   libvirt_cpu_mode=None)

        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        conf = conn.get_guest_config(instance_ref,
                                    _fake_network_info(self.stubs, 1),
                                    None, None)
        self.assertEquals(conf.cpu, None)

    def test_get_guest_cpu_config_host_passthrough_new(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-passthrough")
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, None)
        self.assertEquals(type(conf.cpu),
                          config.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "host-passthrough")
        self.assertEquals(conf.cpu.model, None)

    def test_get_guest_cpu_config_host_model_new(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-model")
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, None)
        self.assertEquals(type(conf.cpu),
                          config.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "host-model")
        self.assertEquals(conf.cpu.model, None)

    def test_get_guest_cpu_config_custom_new(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 11

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="custom")
        self.flags(libvirt_cpu_model="Penryn")
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, None)
        self.assertEquals(type(conf.cpu),
                          config.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, "custom")
        self.assertEquals(conf.cpu.model, "Penryn")

    def test_get_guest_cpu_config_host_passthrough_old(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 7

        self.stubs.Set(libvirt.virConnect, "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-passthrough")
        self.assertRaises(exception.NovaException,
                          conn.get_guest_config,
                          instance_ref,
                          _fake_network_info(self.stubs, 1),
                          None, None)

    def test_get_guest_cpu_config_host_model_old(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 7

        # Ensure we have a predictable host CPU
        def get_host_capabilities_stub(self):
            cpu = config.LibvirtConfigGuestCPU()
            cpu.model = "Opteron_G4"
            cpu.vendor = "AMD"

            caps = config.LibvirtConfigCaps()
            caps.host = config.LibvirtConfigCapsHost()
            caps.host.cpu = cpu
            return caps

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        self.stubs.Set(libvirt_driver.LibvirtDriver,
                       "get_host_capabilities",
                       get_host_capabilities_stub)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="host-model")
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, None)
        self.assertEquals(type(conf.cpu),
                          config.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, None)
        self.assertEquals(conf.cpu.model, "Opteron_G4")
        self.assertEquals(conf.cpu.vendor, "AMD")

    def test_get_guest_cpu_config_custom_old(self):
        def get_lib_version_stub(self):
            return (0 * 1000 * 1000) + (9 * 1000) + 7

        self.stubs.Set(libvirt.virConnect,
                       "getLibVersion",
                       get_lib_version_stub)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, self.test_instance)

        self.flags(libvirt_cpu_mode="custom")
        self.flags(libvirt_cpu_model="Penryn")
        conf = conn.get_guest_config(instance_ref,
                                     _fake_network_info(self.stubs, 1),
                                     None, None)
        self.assertEquals(type(conf.cpu),
                          config.LibvirtConfigGuestCPU)
        self.assertEquals(conf.cpu.mode, None)
        self.assertEquals(conf.cpu.model, "Penryn")

    def test_xml_and_uri_no_ramdisk_no_kernel(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_uri(instance_data,
                                expect_kernel=False, expect_ramdisk=False)

    def test_xml_and_uri_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=True, expect_ramdisk=False)

    def test_xml_and_uri_no_kernel(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=False, expect_ramdisk=False)

    def test_xml_and_uri(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=True, expect_ramdisk=True)

    def test_xml_and_uri_rescue(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=True,
                                expect_ramdisk=True, rescue=instance_data)

    def test_xml_and_uri_rescue_no_kernel_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=False, rescue=instance_data)

    def test_xml_and_uri_rescue_no_kernel(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=True, rescue=instance_data)

    def test_xml_and_uri_rescue_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=True,
                                expect_ramdisk=False, rescue=instance_data)

    def test_xml_uuid(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_uuid(instance_data)

    def test_lxc_container_and_uri(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_container(instance_data)

    def test_xml_disk_prefix(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_disk_prefix(instance_data)

    def test_xml_disk_driver(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_disk_driver(instance_data)

    def test_xml_disk_bus_virtio(self):
        self._check_xml_and_disk_bus({"disk_format": "raw"},
                                     "disk", "virtio")

    def test_xml_disk_bus_ide(self):
        self._check_xml_and_disk_bus({"disk_format": "iso"},
                                     "cdrom", "ide")

    def test_list_instances(self):
        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByID = self.fake_lookup
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 2
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: [0, 1]

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(False)
        instances = conn.list_instances()
        # Only one should be listed, since domain with ID 0 must be skiped
        self.assertEquals(len(instances), 1)

    def test_get_all_block_devices(self):
        xml = [
            # NOTE(vish): id 0 is skipped
            None,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/3'/>
                        </disk>
                    </devices>
                </domain>
            """,
        ]

        def fake_lookup(id):
            return FakeVirtDomain(xml[id])

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 4
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: range(4)
        libvirt_driver.LibvirtDriver._conn.lookupByID = fake_lookup

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(False)
        devices = conn.get_all_block_devices()
        self.assertEqual(devices, ['/path/to/dev/1', '/path/to/dev/3'])

    def test_get_disks(self):
        xml = [
            # NOTE(vish): id 0 is skipped
            None,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                    </devices>
                </domain>
            """,
            """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/3'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                    </devices>
                </domain>
            """,
        ]

        def fake_lookup(id):
            return FakeVirtDomain(xml[id])

        def fake_lookup_name(name):
            return FakeVirtDomain(xml[1])

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.numOfDomains = lambda: 4
        libvirt_driver.LibvirtDriver._conn.listDomainsID = lambda: range(4)
        libvirt_driver.LibvirtDriver._conn.lookupByID = fake_lookup
        libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup_name

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(False)
        devices = conn.get_disks(conn.list_instances()[0])
        self.assertEqual(devices, ['vda', 'vdb'])

    def test_snapshot_in_ami_format(self):
        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign different image_ref from nova/images/fakes for testing ami
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = 'c905cedb-7281-47e4-8a62-f26bc5fc4c77'

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'])

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'ami')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_in_raw_format(self):
        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, self.test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'])

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'raw')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_in_qcow2_format(self):
        self.flags(snapshot_image_format='qcow2',
                   libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, self.test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'])

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['disk_format'], 'qcow2')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_no_image_architecture(self):
        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign different image_ref from nova/images/fakes for
        # testing different base image
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'

        # Assuming that base image already exists in image_service
        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'])

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_snapshot_no_original_image(self):
        self.flags(libvirt_snapshots_directory='./')

        # Start test
        image_service = nova.tests.image.fake.FakeImageService()

        # Assign a non-existent image
        test_instance = copy.deepcopy(self.test_instance)
        test_instance["image_ref"] = '661122aa-1234-dede-fefe-babababababa'

        instance_ref = db.instance_create(self.context, test_instance)
        properties = {'instance_id': instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        snapshot_name = 'test-snap'
        sent_meta = {'name': snapshot_name, 'is_public': False,
                     'status': 'creating', 'properties': properties}
        recv_meta = image_service.create(context, sent_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.StubOutWithMock(libvirt_driver.utils, 'execute')
        libvirt_driver.utils.execute = self.fake_execute

        self.mox.ReplayAll()

        conn = libvirt_driver.LibvirtDriver(False)
        conn.snapshot(self.context, instance_ref, recv_meta['id'])

        snapshot = image_service.show(context, recv_meta['id'])
        self.assertEquals(snapshot['properties']['image_state'], 'available')
        self.assertEquals(snapshot['status'], 'active')
        self.assertEquals(snapshot['name'], snapshot_name)

    def test_attach_invalid_volume_type(self):
        self.create_fake_libvirt_mock()
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(False)
        self.assertRaises(exception.VolumeDriverNotFound,
                          conn.attach_volume,
                          {"driver_volume_type": "badtype"},
                           "fake",
                           "/dev/fake")

    def test_multi_nic(self):
        instance_data = dict(self.test_instance)
        network_info = _fake_network_info(self.stubs, 2)
        conn = libvirt_driver.LibvirtDriver(True)
        instance_ref = db.instance_create(self.context, instance_data)
        xml = conn.to_xml(instance_ref, network_info, None, False)
        tree = etree.fromstring(xml)
        interfaces = tree.findall("./devices/interface")
        self.assertEquals(len(interfaces), 2)
        parameters = interfaces[0].findall('./filterref/parameter')
        self.assertEquals(interfaces[0].get('type'), 'bridge')
        self.assertEquals(parameters[0].get('name'), 'IP')
        self.assertTrue(_ipv4_like(parameters[0].get('value'), '192.168'))

    def _check_xml_and_container(self, instance):
        user_context = context.RequestContext(self.user_id,
                                              self.project_id)
        instance_ref = db.instance_create(user_context, instance)

        self.flags(libvirt_type='lxc')
        conn = libvirt_driver.LibvirtDriver(True)

        self.assertEquals(conn.uri, 'lxc:///')

        network_info = _fake_network_info(self.stubs, 1)
        xml = conn.to_xml(instance_ref, network_info)
        tree = etree.fromstring(xml)

        check = [
        (lambda t: t.find('.').get('type'), 'lxc'),
        (lambda t: t.find('./os/type').text, 'exe'),
        (lambda t: t.find('./devices/filesystem/target').get('dir'), '/')]

        for i, (check, expected_result) in enumerate(check):
            self.assertEqual(check(tree),
                             expected_result,
                             '%s failed common check %d' % (xml, i))

        target = tree.find('./devices/filesystem/source').get('dir')
        self.assertTrue(len(target) > 0)

    def _check_xml_and_disk_prefix(self, instance):
        user_context = context.RequestContext(self.user_id,
                                              self.project_id)
        instance_ref = db.instance_create(user_context, instance)

        type_disk_map = {
            'qemu': [
               (lambda t: t.find('.').get('type'), 'qemu'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'vda')],
            'xen': [
               (lambda t: t.find('.').get('type'), 'xen'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'sda')],
            'kvm': [
               (lambda t: t.find('.').get('type'), 'kvm'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'vda')],
            'uml': [
               (lambda t: t.find('.').get('type'), 'uml'),
               (lambda t: t.find('./devices/disk/target').get('dev'), 'ubda')]
            }

        for (libvirt_type, checks) in type_disk_map.iteritems():
            self.flags(libvirt_type=libvirt_type)
            conn = libvirt_driver.LibvirtDriver(True)

            network_info = _fake_network_info(self.stubs, 1)
            xml = conn.to_xml(instance_ref, network_info)
            tree = etree.fromstring(xml)

            for i, (check, expected_result) in enumerate(checks):
                self.assertEqual(check(tree),
                                 expected_result,
                                 '%s != %s failed check %d' %
                                 (check(tree), expected_result, i))

    def _check_xml_and_disk_driver(self, image_meta):
        os_open = os.open
        directio_supported = True

        def os_open_stub(path, flags, *args, **kwargs):
            if flags & os.O_DIRECT:
                if not directio_supported:
                    raise OSError(errno.EINVAL,
                                  '%s: %s' % (os.strerror(errno.EINVAL), path))
                flags &= ~os.O_DIRECT
            return os_open(path, flags, *args, **kwargs)

        self.stubs.Set(os, 'open', os_open_stub)

        def connection_supports_direct_io_stub(*args, **kwargs):
            return directio_supported

        self.stubs.Set(libvirt_driver.LibvirtDriver,
            '_supports_direct_io', connection_supports_direct_io_stub)

        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, self.test_instance)
        network_info = _fake_network_info(self.stubs, 1)

        xml = libvirt_driver.LibvirtDriver(True).to_xml(instance_ref,
                                                        network_info,
                                                        image_meta)
        tree = etree.fromstring(xml)
        disks = tree.findall('./devices/disk/driver')
        for disk in disks:
            self.assertEqual(disk.get("cache"), "none")

        directio_supported = False

        # The O_DIRECT availability is cached on first use in
        # LibvirtDriver, hence we re-create it here
        xml = libvirt_driver.LibvirtDriver(True).to_xml(instance_ref,
                                                        network_info,
                                                        image_meta)
        tree = etree.fromstring(xml)
        disks = tree.findall('./devices/disk/driver')
        for disk in disks:
            self.assertEqual(disk.get("cache"), "writethrough")

    def _check_xml_and_disk_bus(self, image_meta, device_type, bus):
        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, self.test_instance)
        network_info = _fake_network_info(self.stubs, 1)

        xml = libvirt_driver.LibvirtDriver(True).to_xml(instance_ref,
                                                        network_info,
                                                        image_meta)
        tree = etree.fromstring(xml)
        self.assertEqual(tree.find('./devices/disk').get('device'),
                         device_type)
        self.assertEqual(tree.find('./devices/disk/target').get('bus'), bus)

    def _check_xml_and_uuid(self, image_meta):
        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, self.test_instance)
        network_info = _fake_network_info(self.stubs, 1)

        xml = libvirt_driver.LibvirtDriver(True).to_xml(instance_ref,
                                                        network_info,
                                                        image_meta)
        tree = etree.fromstring(xml)
        self.assertEqual(tree.find('./uuid').text,
                         instance_ref['uuid'])

    def _check_xml_and_uri(self, instance, expect_ramdisk, expect_kernel,
                           rescue=None):
        user_context = context.RequestContext(self.user_id, self.project_id)
        instance_ref = db.instance_create(user_context, instance)
        network_ref = db.project_get_networks(context.get_admin_context(),
                                             self.project_id)[0]

        type_uri_map = {'qemu': ('qemu:///system',
                             [(lambda t: t.find('.').get('type'), 'qemu'),
                              (lambda t: t.find('./os/type').text, 'hvm'),
                              (lambda t: t.find('./devices/emulator'), None)]),
                        'kvm': ('qemu:///system',
                             [(lambda t: t.find('.').get('type'), 'kvm'),
                              (lambda t: t.find('./os/type').text, 'hvm'),
                              (lambda t: t.find('./devices/emulator'), None)]),
                        'uml': ('uml:///system',
                             [(lambda t: t.find('.').get('type'), 'uml'),
                              (lambda t: t.find('./os/type').text, 'uml')]),
                        'xen': ('xen:///',
                             [(lambda t: t.find('.').get('type'), 'xen'),
                              (lambda t: t.find('./os/type').text, 'linux')]),
                              }

        for hypervisor_type in ['qemu', 'kvm', 'xen']:
            check_list = type_uri_map[hypervisor_type][1]

            if rescue:
                suffix = '.rescue'
            else:
                suffix = ''
            if expect_kernel:
                check = (lambda t: t.find('./os/kernel').text.split(
                    '/')[1], 'kernel' + suffix)
            else:
                check = (lambda t: t.find('./os/kernel'), None)
            check_list.append(check)

            if expect_ramdisk:
                check = (lambda t: t.find('./os/initrd').text.split(
                    '/')[1], 'ramdisk' + suffix)
            else:
                check = (lambda t: t.find('./os/initrd'), None)
            check_list.append(check)

            if hypervisor_type in ['qemu', 'kvm']:
                check = (lambda t: t.findall('./devices/serial')[0].get(
                        'type'), 'file')
                check_list.append(check)
                check = (lambda t: t.findall('./devices/serial')[1].get(
                        'type'), 'pty')
                check_list.append(check)
                check = (lambda t: t.findall('./devices/serial/source')[0].get(
                        'path').split('/')[1], 'console.log')
                check_list.append(check)
            else:
                check = (lambda t: t.find('./devices/console').get(
                        'type'), 'pty')
                check_list.append(check)

        parameter = './devices/interface/filterref/parameter'
        common_checks = [
            (lambda t: t.find('.').tag, 'domain'),
            (lambda t: t.find(parameter).get('name'), 'IP'),
            (lambda t: _ipv4_like(t.find(parameter).get('value'), '192.168'),
             True),
            (lambda t: t.find('./memory').text, '2097152')]
        if rescue:
            common_checks += [
                (lambda t: t.findall('./devices/disk/source')[0].get(
                    'file').split('/')[1], 'disk.rescue'),
                (lambda t: t.findall('./devices/disk/source')[1].get(
                    'file').split('/')[1], 'disk')]
        else:
            common_checks += [(lambda t: t.findall(
                './devices/disk/source')[0].get('file').split('/')[1],
                               'disk')]
            common_checks += [(lambda t: t.findall(
                './devices/disk/source')[1].get('file').split('/')[1],
                               'disk.local')]

        for (libvirt_type, (expected_uri, checks)) in type_uri_map.iteritems():
            self.flags(libvirt_type=libvirt_type)
            conn = libvirt_driver.LibvirtDriver(True)

            self.assertEquals(conn.uri, expected_uri)

            network_info = _fake_network_info(self.stubs, 1)
            xml = conn.to_xml(instance_ref, network_info, None, rescue)
            tree = etree.fromstring(xml)
            for i, (check, expected_result) in enumerate(checks):
                self.assertEqual(check(tree),
                                 expected_result,
                                 '%s != %s failed check %d' %
                                 (check(tree), expected_result, i))

            for i, (check, expected_result) in enumerate(common_checks):
                self.assertEqual(check(tree),
                                 expected_result,
                                 '%s != %s failed common check %d' %
                                 (check(tree), expected_result, i))

        # This test is supposed to make sure we don't
        # override a specifically set uri
        #
        # Deliberately not just assigning this string to FLAGS.libvirt_uri and
        # checking against that later on. This way we make sure the
        # implementation doesn't fiddle around with the FLAGS.
        testuri = 'something completely different'
        self.flags(libvirt_uri=testuri)
        for (libvirt_type, (expected_uri, checks)) in type_uri_map.iteritems():
            self.flags(libvirt_type=libvirt_type)
            conn = libvirt_driver.LibvirtDriver(True)
            self.assertEquals(conn.uri, testuri)
        db.instance_destroy(user_context, instance_ref['uuid'])

    def test_ensure_filtering_rules_for_instance_timeout(self):
        """ensure_filtering_fules_for_instance() finishes with timeout."""
        # Preparing mocks
        def fake_none(self, *args):
            return

        def fake_raise(self):
            raise libvirt.libvirtError('ERR')

        class FakeTime(object):
            def __init__(self):
                self.counter = 0

            def sleep(self, t):
                self.counter += t

        fake_timer = FakeTime()

        # _fake_network_info must be called before create_fake_libvirt_mock(),
        # as _fake_network_info calls importutils.import_class() and
        # create_fake_libvirt_mock() mocks importutils.import_class().
        network_info = _fake_network_info(self.stubs, 1)
        self.create_fake_libvirt_mock()
        instance_ref = db.instance_create(self.context, self.test_instance)

        # Start test
        self.mox.ReplayAll()
        try:
            conn = libvirt_driver.LibvirtDriver(False)
            self.stubs.Set(conn.firewall_driver,
                           'setup_basic_filtering',
                           fake_none)
            self.stubs.Set(conn.firewall_driver,
                           'prepare_instance_filter',
                           fake_none)
            self.stubs.Set(conn.firewall_driver,
                           'instance_filter_exists',
                           fake_none)
            conn.ensure_filtering_rules_for_instance(instance_ref,
                                                     network_info,
                                                     time=fake_timer)
        except exception.NovaException, e:
            c1 = (0 <= str(e).find('Timeout migrating for'))
        self.assertTrue(c1)

        self.assertEqual(29, fake_timer.counter, "Didn't wait the expected "
                                                 "amount of time")

        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_check_can_live_migrate_dest_all_pass_with_block_migration(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, '_get_compute_info')
        self.mox.StubOutWithMock(conn, 'get_instance_disk_info')
        self.mox.StubOutWithMock(conn, '_create_shared_storage_test_file')
        self.mox.StubOutWithMock(conn, '_compare_cpu')

        conn._get_compute_info(self.context, FLAGS.host).AndReturn(
                                              {'disk_available_least': 400})
        conn.get_instance_disk_info(instance_ref["name"]).AndReturn(
                                            '[{"virt_disk_size":2}]')
        # _check_cpu_match
        conn._get_compute_info(self.context,
                               src).AndReturn({'cpu_info': "asdf"})
        conn._compare_cpu("asdf")

        # mounted_on_same_shared_storage
        filename = "file"
        conn._create_shared_storage_test_file().AndReturn(filename)

        self.mox.ReplayAll()
        return_value = conn.check_can_live_migrate_destination(self.context,
                                instance_ref, True, False)
        self.assertDictEqual(return_value,
                             {"filename": "file", "block_migration": True})

    def test_check_can_live_migrate_dest_all_pass_no_block_migration(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, '_get_compute_info')
        self.mox.StubOutWithMock(conn, '_create_shared_storage_test_file')
        self.mox.StubOutWithMock(conn, '_compare_cpu')

        # _check_cpu_match
        conn._get_compute_info(self.context,
                               src).AndReturn({'cpu_info': "asdf"})
        conn._compare_cpu("asdf")

        # mounted_on_same_shared_storage
        filename = "file"
        conn._create_shared_storage_test_file().AndReturn(filename)

        self.mox.ReplayAll()
        return_value = conn.check_can_live_migrate_destination(self.context,
                                instance_ref, False, False)
        self.assertDictEqual(return_value,
                            {"filename": "file", "block_migration": False})

    def test_check_can_live_migrate_dest_fails_not_enough_disk(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, '_get_compute_info')
        self.mox.StubOutWithMock(conn, 'get_instance_disk_info')

        conn._get_compute_info(self.context, FLAGS.host).AndReturn(
                                              {'disk_available_least': 0})
        conn.get_instance_disk_info(instance_ref["name"]).AndReturn(
                                            '[{"virt_disk_size":2}]')

        self.mox.ReplayAll()
        self.assertRaises(exception.MigrationError,
                          conn.check_can_live_migrate_destination,
                          self.context, instance_ref, True, False)

    def test_check_can_live_migrate_dest_incompatible_cpu_raises(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, '_get_compute_info')
        self.mox.StubOutWithMock(conn, '_compare_cpu')

        conn._get_compute_info(self.context, src).AndReturn(
                {'cpu_info': "asdf"})
        conn._compare_cpu("asdf").AndRaise(exception.InvalidCPUInfo)

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidCPUInfo,
                          conn.check_can_live_migrate_destination,
                          self.context, instance_ref, False, False)

    def test_check_can_live_migrate_dest_fail_space_with_block_migration(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest = "fake_host_2"
        src = instance_ref['host']
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, '_get_compute_info')
        self.mox.StubOutWithMock(conn, 'get_instance_disk_info')

        conn._get_compute_info(self.context, FLAGS.host).AndReturn(
                                              {'disk_available_least': 0})
        conn.get_instance_disk_info(instance_ref["name"]).AndReturn(
                                            '[{"virt_disk_size":2}]')

        self.mox.ReplayAll()
        self.assertRaises(exception.MigrationError,
                          conn.check_can_live_migrate_destination,
                          self.context, instance_ref, True, False)

    def test_check_can_live_migrate_dest_cleanup_works_correctly(self):
        dest_check_data = {"filename": "file", "block_migration": True}
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, '_cleanup_shared_storage_test_file')
        conn._cleanup_shared_storage_test_file("file")

        self.mox.ReplayAll()
        conn.check_can_live_migrate_destination_cleanup(self.context,
                                                        dest_check_data)

    def test_check_can_live_migrate_source_works_correctly(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file", "block_migration": True}
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(False)

        self.mox.ReplayAll()
        conn.check_can_live_migrate_source(self.context, instance_ref,
                                           dest_check_data)

    def test_check_can_live_migrate_dest_fail_shared_storage_with_blockm(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file", "block_migration": True}
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(True)

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSharedStorage,
                          conn.check_can_live_migrate_source,
                          self.context, instance_ref, dest_check_data)

    def test_check_can_live_migrate_no_shared_storage_no_blck_mig_raises(self):
        instance_ref = db.instance_create(self.context, self.test_instance)
        dest_check_data = {"filename": "file", "block_migration": False}
        conn = libvirt_driver.LibvirtDriver(False)

        self.mox.StubOutWithMock(conn, "_check_shared_storage_test_file")
        conn._check_shared_storage_test_file("file").AndReturn(False)

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSharedStorage,
                          conn.check_can_live_migrate_source,
                          self.context, instance_ref, dest_check_data)

    def test_live_migration_raises_exception(self):
        """Confirms recover method is called when exceptions are raised."""
        # Preparing data
        self.compute = importutils.import_object(FLAGS.compute_manager)
        instance_dict = {'host': 'fake',
                         'power_state': power_state.RUNNING,
                         'vm_state': vm_states.ACTIVE}
        instance_ref = db.instance_create(self.context, self.test_instance)
        instance_ref = db.instance_update(self.context, instance_ref['uuid'],
                                          instance_dict)
        vol_dict = {'status': 'migrating', 'size': 1}
        volume_ref = db.volume_create(self.context, vol_dict)
        db.volume_attached(self.context,
                           volume_ref['id'],
                           instance_ref['uuid'],
                           '/dev/fake')

        # Preparing mocks
        vdmock = self.mox.CreateMock(libvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "migrateToURI")
        _bandwidth = FLAGS.live_migration_bandwidth
        vdmock.migrateToURI(FLAGS.live_migration_uri % 'dest',
                            mox.IgnoreArg(),
                            None,
                            _bandwidth).AndRaise(libvirt.libvirtError('ERR'))

        def fake_lookup(instance_name):
            if instance_name == instance_ref.name:
                return vdmock

        self.create_fake_libvirt_mock(lookupByName=fake_lookup)
        self.mox.StubOutWithMock(self.compute, "rollback_live_migration")
        self.compute.rollback_live_migration(self.context, instance_ref,
                                            'dest', False)

        #start test
        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(False)
        self.assertRaises(libvirt.libvirtError,
                      conn._live_migration,
                      self.context, instance_ref, 'dest', False,
                      self.compute.rollback_live_migration)

        instance_ref = db.instance_get(self.context, instance_ref['id'])
        self.assertTrue(instance_ref['vm_state'] == vm_states.ACTIVE)
        self.assertTrue(instance_ref['power_state'] == power_state.RUNNING)
        volume_ref = db.volume_get(self.context, volume_ref['id'])
        self.assertTrue(volume_ref['status'] == 'in-use')

        db.volume_destroy(self.context, volume_ref['id'])
        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_pre_live_migration_works_correctly_mocked(self):
        """Confirms pre_block_migration works correctly."""
        # Creating testdata
        vol = {'block_device_mapping': [
                  {'connection_info': 'dummy', 'mount_device': '/dev/sda'},
                  {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}]}
        conn = libvirt_driver.LibvirtDriver(False)

        class FakeNetworkInfo():
            def fixed_ips(self):
                return ["test_ip_addr"]

        inst_ref = {'id': 'foo'}
        c = context.get_admin_context()
        nw_info = FakeNetworkInfo()

        # Creating mocks
        self.mox.StubOutWithMock(driver, "block_device_info_get_mapping")
        driver.block_device_info_get_mapping(vol
            ).AndReturn(vol['block_device_mapping'])
        self.mox.StubOutWithMock(conn, "volume_driver_method")
        for v in vol['block_device_mapping']:
            conn.volume_driver_method('connect_volume',
                                     v['connection_info'],
                                      v['mount_device'].rpartition("/")[2])
        self.mox.StubOutWithMock(conn, 'plug_vifs')
        conn.plug_vifs(mox.IsA(inst_ref), nw_info)

        self.mox.ReplayAll()
        result = conn.pre_live_migration(c, inst_ref, vol, nw_info)
        self.assertEqual(result, None)

    def test_pre_block_migration_works_correctly(self):
        """Confirms pre_block_migration works correctly."""
        # Replace instances_path since this testcase creates tmpfile
        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            # Test data
            instance_ref = db.instance_create(self.context, self.test_instance)
            dummyjson = ('[{"path": "%s/disk", "disk_size": "10737418240",'
                         ' "type": "raw", "backing_file": ""}]')

            # Preparing mocks
            # qemu-img should be mockd since test environment might not have
            # large disk space.
            self.mox.ReplayAll()
            conn = libvirt_driver.LibvirtDriver(False)
            conn.pre_block_migration(self.context, instance_ref,
                                     dummyjson % tmpdir)

            self.assertTrue(os.path.exists('%s/%s/' %
                                           (tmpdir, instance_ref.name)))

        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_get_instance_disk_info_works_correctly(self):
        """Confirms pre_block_migration works correctly."""
        # Test data
        instance_ref = db.instance_create(self.context, self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")

        ret = ("image: /test/disk\n"
               "file format: raw\n"
               "virtual size: 20G (21474836480 bytes)\n"
               "disk size: 3.1G\n"
               "cluster_size: 2097152\n"
               "backing file: /test/dummy (actual path: /backing/file)\n")

        # Preparing mocks
        vdmock = self.mox.CreateMock(libvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance_ref.name:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        GB = 1024 * 1024 * 1024
        fake_libvirt_utils.disk_sizes['/test/disk'] = 10 * GB
        fake_libvirt_utils.disk_sizes['/test/disk.local'] = 20 * GB
        fake_libvirt_utils.disk_backing_files['/test/disk.local'] = 'file'

        self.mox.StubOutWithMock(os.path, "getsize")
        os.path.getsize('/test/disk').AndReturn((10737418240))

        self.mox.StubOutWithMock(utils, "execute")
        utils.execute('qemu-img', 'info',
                      '/test/disk.local').AndReturn((ret, ''))

        os.path.getsize('/test/disk.local').AndReturn((21474836480))

        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(False)
        info = conn.get_instance_disk_info(instance_ref.name)
        info = jsonutils.loads(info)
        self.assertEquals(info[0]['type'], 'raw')
        self.assertEquals(info[0]['path'], '/test/disk')
        self.assertEquals(info[0]['disk_size'], 10737418240)
        self.assertEquals(info[0]['backing_file'], "")
        self.assertEquals(info[1]['type'], 'qcow2')
        self.assertEquals(info[1]['path'], '/test/disk.local')
        self.assertEquals(info[1]['virt_disk_size'], 21474836480)
        self.assertEquals(info[1]['backing_file'], "file")

        db.instance_destroy(self.context, instance_ref['uuid'])

    def test_spawn_with_network_info(self):
        # Preparing mocks
        def fake_none(self, instance):
            return

        # _fake_network_info must be called before create_fake_libvirt_mock(),
        # as _fake_network_info calls importutils.import_class() and
        # create_fake_libvirt_mock() mocks importutils.import_class().
        network_info = _fake_network_info(self.stubs, 1)
        self.create_fake_libvirt_mock()

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 123456  # we send an int to test sha1 call
        instance = db.instance_create(self.context, instance_ref)

        # Start test
        self.mox.ReplayAll()
        conn = libvirt_driver.LibvirtDriver(False)
        self.stubs.Set(conn.firewall_driver,
                       'setup_basic_filtering',
                       fake_none)
        self.stubs.Set(conn.firewall_driver,
                       'prepare_instance_filter',
                       fake_none)

        try:
            conn.spawn(self.context, instance, None, network_info)
        except Exception, e:
            # assert that no exception is raised due to sha1 receiving an int
            self.assertEqual(-1, str(e.message).find('must be string or buffer'
                                                     ', not int'))
            count = (0 <= str(e.message).find('Unexpected method call'))

        path = os.path.join(FLAGS.instances_path, instance.name)
        if os.path.isdir(path):
            shutil.rmtree(path)

        path = os.path.join(FLAGS.instances_path, FLAGS.base_dir_name)
        if os.path.isdir(path):
            shutil.rmtree(os.path.join(FLAGS.instances_path,
                                       FLAGS.base_dir_name))

    def test_get_console_output_file(self):

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            instance_ref = self.test_instance
            instance_ref['image_ref'] = 123456
            instance = db.instance_create(self.context, instance_ref)

            console_dir = (os.path.join(tmpdir, instance['name']))
            os.mkdir(console_dir)
            console_log = '%s/console.log' % (console_dir)
            f = open(console_log, "w")
            f.write("foo")
            f.close()
            fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <console type='file'>
                            <source path='%s'/>
                            <target port='0'/>
                        </console>
                    </devices>
                </domain>
            """ % console_log

            def fake_lookup(id):
                return FakeVirtDomain(fake_dom_xml)

            self.create_fake_libvirt_mock()
            libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup
            libvirt_driver.libvirt_utils = fake_libvirt_utils

            conn = libvirt_driver.LibvirtDriver(False)
            output = conn.get_console_output(instance)
            self.assertEquals("foo", output)

    def test_get_console_output_pty(self):

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            instance_ref = self.test_instance
            instance_ref['image_ref'] = 123456
            instance = db.instance_create(self.context, instance_ref)

            console_dir = (os.path.join(tmpdir, instance['name']))
            os.mkdir(console_dir)
            pty_file = '%s/fake_pty' % (console_dir)
            f = open(pty_file, "w")
            f.write("foo")
            f.close()
            fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <console type='pty'>
                            <source path='%s'/>
                            <target port='0'/>
                        </console>
                    </devices>
                </domain>
            """ % pty_file

            def fake_lookup(id):
                return FakeVirtDomain(fake_dom_xml)

            def _fake_flush(self, fake_pty):
                with open(fake_pty, 'r+') as fp:
                    return fp.read()

            self.create_fake_libvirt_mock()
            libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup
            libvirt_driver.LibvirtDriver._flush_libvirt_console = _fake_flush
            libvirt_driver.libvirt_utils = fake_libvirt_utils

            conn = libvirt_driver.LibvirtDriver(False)
            output = conn.get_console_output(instance)
            self.assertEquals("foo", output)

    def test_get_host_ip_addr(self):
        conn = libvirt_driver.LibvirtDriver(False)
        ip = conn.get_host_ip_addr()
        self.assertEquals(ip, FLAGS.my_ip)

    def test_broken_connection(self):
        for (error, domain) in (
                (libvirt.VIR_ERR_SYSTEM_ERROR, libvirt.VIR_FROM_REMOTE),
                (libvirt.VIR_ERR_SYSTEM_ERROR, libvirt.VIR_FROM_RPC)):

            conn = libvirt_driver.LibvirtDriver(False)

            self.mox.StubOutWithMock(conn, "_wrapped_conn")
            self.mox.StubOutWithMock(conn._wrapped_conn, "getCapabilities")
            self.mox.StubOutWithMock(libvirt.libvirtError, "get_error_code")
            self.mox.StubOutWithMock(libvirt.libvirtError, "get_error_domain")

            conn._wrapped_conn.getCapabilities().AndRaise(
                    libvirt.libvirtError("fake failure"))

            libvirt.libvirtError.get_error_code().AndReturn(error)
            libvirt.libvirtError.get_error_domain().AndReturn(domain)

            self.mox.ReplayAll()

            self.assertFalse(conn._test_connection())

            self.mox.UnsetStubs()

    def test_volume_in_mapping(self):
        conn = libvirt_driver.LibvirtDriver(False)
        swap = {'device_name': '/dev/sdb',
                'swap_size': 1}
        ephemerals = [{'num': 0,
                       'virtual_name': 'ephemeral0',
                       'device_name': '/dev/sdc1',
                       'size': 1},
                      {'num': 2,
                       'virtual_name': 'ephemeral2',
                       'device_name': '/dev/sdd',
                       'size': 1}]
        block_device_mapping = [{'mount_device': '/dev/sde',
                                 'device_path': 'fake_device'},
                                {'mount_device': '/dev/sdf',
                                 'device_path': 'fake_device'}]
        block_device_info = {
                'root_device_name': '/dev/sda',
                'swap': swap,
                'ephemerals': ephemerals,
                'block_device_mapping': block_device_mapping}

        def _assert_volume_in_mapping(device_name, true_or_false):
            self.assertEquals(conn._volume_in_mapping(device_name,
                                                      block_device_info),
                              true_or_false)

        _assert_volume_in_mapping('sda', False)
        _assert_volume_in_mapping('sdb', True)
        _assert_volume_in_mapping('sdc1', True)
        _assert_volume_in_mapping('sdd', True)
        _assert_volume_in_mapping('sde', True)
        _assert_volume_in_mapping('sdf', True)
        _assert_volume_in_mapping('sdg', False)
        _assert_volume_in_mapping('sdh1', False)

    def test_immediate_delete(self):
        def fake_lookup_by_name(instance_name):
            raise exception.InstanceNotFound()

        conn = libvirt_driver.LibvirtDriver(False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)

        instance = db.instance_create(self.context, self.test_instance)
        conn.destroy(instance, {})

    def test_destroy_saved(self):
        """Ensure destroy calls managedSaveRemove for saved instance"""
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.destroy()
        mock.hasManagedSaveImage(0).AndReturn(1)
        mock.managedSaveRemove(0)
        mock.undefine()

        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            return {'state': power_state.SHUTDOWN}

        conn = libvirt_driver.LibvirtDriver(False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        conn.destroy(instance, [])

    def test_private_destroy(self):
        """Ensure Instance not found skips undefine"""
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.destroy()
        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            return {'state': power_state.SHUTDOWN}

        conn = libvirt_driver.LibvirtDriver(False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        result = conn._destroy(instance)
        self.assertTrue(result)

    def test_private_destroy_not_found(self):
        """Ensure Instance not found skips undefine"""
        mock = self.mox.CreateMock(libvirt.virDomain)
        mock.destroy()
        self.mox.ReplayAll()

        def fake_lookup_by_name(instance_name):
            return mock

        def fake_get_info(instance_name):
            raise exception.InstanceNotFound()

        conn = libvirt_driver.LibvirtDriver(False)
        self.stubs.Set(conn, '_lookup_by_name', fake_lookup_by_name)
        self.stubs.Set(conn, 'get_info', fake_get_info)
        instance = {"name": "instancename", "id": "instanceid",
                    "uuid": "875a8070-d0b9-4949-8b31-104d125c9a64"}
        result = conn._destroy(instance)
        self.assertFalse(result)

    def test_available_least_handles_missing(self):
        """Ensure destroy calls managedSaveRemove for saved instance"""
        conn = libvirt_driver.LibvirtDriver(False)

        def list_instances():
            return ['fake']
        self.stubs.Set(conn, 'list_instances', list_instances)

        def get_info(instance_name):
            raise exception.InstanceNotFound()
        self.stubs.Set(conn, 'get_instance_disk_info', get_info)

        result = conn.get_disk_available_least()
        space = fake_libvirt_utils.get_fs_info(FLAGS.instances_path)['free']
        self.assertEqual(result, space / 1024 ** 3)

    def test_cpu_info(self):
        conn = libvirt_driver.LibvirtDriver(True)

        def get_host_capabilities_stub(self):
            cpu = config.LibvirtConfigCPU()
            cpu.model = "Opteron_G4"
            cpu.vendor = "AMD"
            cpu.arch = "x86_64"

            cpu.cores = 2
            cpu.threads = 1
            cpu.sockets = 4

            cpu.add_feature(config.LibvirtConfigCPUFeature("extapic"))
            cpu.add_feature(config.LibvirtConfigCPUFeature("3dnow"))

            caps = config.LibvirtConfigCaps()
            caps.host = config.LibvirtConfigCapsHost()
            caps.host.cpu = cpu

            guest = config.LibvirtConfigGuest()
            guest.ostype = "hvm"
            guest.arch = "x86_64"
            caps.guests.append(guest)

            guest = config.LibvirtConfigGuest()
            guest.ostype = "hvm"
            guest.arch = "i686"
            caps.guests.append(guest)

            return caps

        self.stubs.Set(libvirt_driver.LibvirtDriver,
                       'get_host_capabilities',
                       get_host_capabilities_stub)

        want = {"vendor": "AMD",
                "features": ["extapic", "3dnow"],
                "permitted_instance_types": ["x86_64", "i686"],
                "model": "Opteron_G4",
                "arch": "x86_64",
                "topology": {"cores": 2, "threads": 1, "sockets": 4}}
        got = jsonutils.loads(conn.get_cpu_info())
        self.assertEqual(want, got)


class HostStateTestCase(test.TestCase):

    cpu_info = ('{"vendor": "Intel", "model": "pentium", "arch": "i686", '
                 '"features": ["ssse3", "monitor", "pni", "sse2", "sse", '
                 '"fxsr", "clflush", "pse36", "pat", "cmov", "mca", "pge", '
                 '"mtrr", "sep", "apic"], '
                 '"topology": {"cores": "1", "threads": "1", "sockets": "1"}}')

    class FakeConnection(object):
        """Fake connection object"""

        def get_vcpu_total(self):
            return 1

        def get_vcpu_used(self):
            return 0

        def get_cpu_info(self):
            return HostStateTestCase.cpu_info

        def get_local_gb_total(self):
            return 100

        def get_local_gb_used(self):
            return 20

        def get_memory_mb_total(self):
            return 497

        def get_memory_mb_used(self):
            return 88

        def get_hypervisor_type(self):
            return 'QEMU'

        def get_hypervisor_version(self):
            return 13091

        def get_hypervisor_hostname(self):
            return 'compute1'

        def get_disk_available_least(self):
            return 13091

    def test_update_status(self):
        self.mox.StubOutWithMock(libvirt_driver, 'LibvirtDriver')
        libvirt_driver.LibvirtDriver(True).AndReturn(self.FakeConnection())

        self.mox.ReplayAll()
        hs = libvirt_driver.HostState(True)
        stats = hs._stats
        self.assertEquals(stats["vcpus"], 1)
        self.assertEquals(stats["vcpus_used"], 0)
        self.assertEquals(stats["cpu_info"],
                {"vendor": "Intel", "model": "pentium", "arch": "i686",
                 "features": ["ssse3", "monitor", "pni", "sse2", "sse",
                              "fxsr", "clflush", "pse36", "pat", "cmov",
                              "mca", "pge", "mtrr", "sep", "apic"],
                 "topology": {"cores": "1", "threads": "1", "sockets": "1"}
                })
        self.assertEquals(stats["disk_total"], 100)
        self.assertEquals(stats["disk_used"], 20)
        self.assertEquals(stats["disk_available"], 80)
        self.assertEquals(stats["host_memory_total"], 497)
        self.assertEquals(stats["host_memory_free"], 409)
        self.assertEquals(stats["hypervisor_type"], 'QEMU')
        self.assertEquals(stats["hypervisor_version"], 13091)
        self.assertEquals(stats["hypervisor_hostname"], 'compute1')


class NWFilterFakes:
    def __init__(self):
        self.filters = {}

    def nwfilterLookupByName(self, name):
        if name in self.filters:
            return self.filters[name]
        raise libvirt.libvirtError('Filter Not Found')

    def filterDefineXMLMock(self, xml):
        class FakeNWFilterInternal:
            def __init__(self, parent, name):
                self.name = name
                self.parent = parent

            def undefine(self):
                del self.parent.filters[self.name]
                pass
        tree = etree.fromstring(xml)
        name = tree.get('name')
        if name not in self.filters:
            self.filters[name] = FakeNWFilterInternal(self, name)
        return True


class IptablesFirewallTestCase(test.TestCase):
    def setUp(self):
        super(IptablesFirewallTestCase, self).setUp()

        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id, self.project_id)

        class FakeLibvirtDriver(object):
            def nwfilterDefineXML(*args, **kwargs):
                """setup_basic_rules in nwfilter calls this."""
                pass
        self.fake_libvirt_connection = FakeLibvirtDriver()
        self.fw = firewall.IptablesFirewallDriver(
                      get_connection=lambda: self.fake_libvirt_connection)

    in_nat_rules = [
      '# Generated by iptables-save v1.4.10 on Sat Feb 19 00:03:19 2011',
      '*nat',
      ':PREROUTING ACCEPT [1170:189210]',
      ':INPUT ACCEPT [844:71028]',
      ':OUTPUT ACCEPT [5149:405186]',
      ':POSTROUTING ACCEPT [5063:386098]',
    ]

    in_filter_rules = [
      '# Generated by iptables-save v1.4.4 on Mon Dec  6 11:54:13 2010',
      '*filter',
      ':INPUT ACCEPT [969615:281627771]',
      ':FORWARD ACCEPT [0:0]',
      ':OUTPUT ACCEPT [915599:63811649]',
      ':nova-block-ipv4 - [0:0]',
      '-A INPUT -i virbr0 -p tcp -m tcp --dport 67 -j ACCEPT ',
      '-A FORWARD -d 192.168.122.0/24 -o virbr0 -m state --state RELATED'
      ',ESTABLISHED -j ACCEPT ',
      '-A FORWARD -s 192.168.122.0/24 -i virbr0 -j ACCEPT ',
      '-A FORWARD -i virbr0 -o virbr0 -j ACCEPT ',
      '-A FORWARD -o virbr0 -j REJECT --reject-with icmp-port-unreachable ',
      '-A FORWARD -i virbr0 -j REJECT --reject-with icmp-port-unreachable ',
      'COMMIT',
      '# Completed on Mon Dec  6 11:54:13 2010',
    ]

    in6_filter_rules = [
      '# Generated by ip6tables-save v1.4.4 on Tue Jan 18 23:47:56 2011',
      '*filter',
      ':INPUT ACCEPT [349155:75810423]',
      ':FORWARD ACCEPT [0:0]',
      ':OUTPUT ACCEPT [349256:75777230]',
      'COMMIT',
      '# Completed on Tue Jan 18 23:47:56 2011',
    ]

    def _create_instance_ref(self):
        return db.instance_create(self.context,
                                  {'user_id': 'fake',
                                   'project_id': 'fake',
                                   'instance_type_id': 1})

    def test_static_filters(self):
        instance_ref = self._create_instance_ref()
        src_instance_ref = self._create_instance_ref()

        admin_ctxt = context.get_admin_context()
        secgroup = db.security_group_create(admin_ctxt,
                                            {'user_id': 'fake',
                                             'project_id': 'fake',
                                             'name': 'testgroup',
                                             'description': 'test group'})

        src_secgroup = db.security_group_create(admin_ctxt,
                                                {'user_id': 'fake',
                                                 'project_id': 'fake',
                                                 'name': 'testsourcegroup',
                                                 'description': 'src group'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'icmp',
                                       'from_port': -1,
                                       'to_port': -1,
                                       'cidr': '192.168.11.0/24'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'icmp',
                                       'from_port': 8,
                                       'to_port': -1,
                                       'cidr': '192.168.11.0/24'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'tcp',
                                       'from_port': 80,
                                       'to_port': 81,
                                       'cidr': '192.168.10.0/24'})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'protocol': 'tcp',
                                       'from_port': 80,
                                       'to_port': 81,
                                       'group_id': src_secgroup['id']})

        db.security_group_rule_create(admin_ctxt,
                                      {'parent_group_id': secgroup['id'],
                                       'group_id': src_secgroup['id']})

        db.instance_add_security_group(admin_ctxt, instance_ref['uuid'],
                                       secgroup['id'])
        db.instance_add_security_group(admin_ctxt, src_instance_ref['uuid'],
                                       src_secgroup['id'])
        instance_ref = db.instance_get(admin_ctxt, instance_ref['id'])
        src_instance_ref = db.instance_get(admin_ctxt, src_instance_ref['id'])

#        self.fw.add_instance(instance_ref)
        def fake_iptables_execute(*cmd, **kwargs):
            process_input = kwargs.get('process_input', None)
            if cmd == ('ip6tables-save', '-t', 'filter'):
                return '\n'.join(self.in6_filter_rules), None
            if cmd == ('iptables-save', '-t', 'filter'):
                return '\n'.join(self.in_filter_rules), None
            if cmd == ('iptables-save', '-t', 'nat'):
                return '\n'.join(self.in_nat_rules), None
            if cmd == ('iptables-restore',):
                lines = process_input.split('\n')
                if '*filter' in lines:
                    self.out_rules = lines
                return '', ''
            if cmd == ('ip6tables-restore',):
                lines = process_input.split('\n')
                if '*filter' in lines:
                    self.out6_rules = lines
                return '', ''
            print cmd, kwargs

        network_model = _fake_network_info(self.stubs, 1, spectacular=True)

        from nova.network import linux_net
        linux_net.iptables_manager.execute = fake_iptables_execute

        _fake_stub_out_get_nw_info(self.stubs, lambda *a, **kw: network_model)

        network_info = network_model.legacy()
        self.fw.prepare_instance_filter(instance_ref, network_info)
        self.fw.apply_instance_filter(instance_ref, network_info)

        in_rules = filter(lambda l: not l.startswith('#'),
                          self.in_filter_rules)
        for rule in in_rules:
            if not 'nova' in rule:
                self.assertTrue(rule in self.out_rules,
                                'Rule went missing: %s' % rule)

        instance_chain = None
        for rule in self.out_rules:
            # This is pretty crude, but it'll do for now
            # last two octets change
            if re.search('-d 192.168.[0-9]{1,3}.[0-9]{1,3} -j', rule):
                instance_chain = rule.split(' ')[-1]
                break
        self.assertTrue(instance_chain, "The instance chain wasn't added")

        security_group_chain = None
        for rule in self.out_rules:
            # This is pretty crude, but it'll do for now
            if '-A %s -j' % instance_chain in rule:
                security_group_chain = rule.split(' ')[-1]
                break
        self.assertTrue(security_group_chain,
                        "The security group chain wasn't added")

        regex = re.compile('-A .* -j ACCEPT -p icmp -s 192.168.11.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "ICMP acceptance rule wasn't added")

        regex = re.compile('-A .* -j ACCEPT -p icmp -m icmp --icmp-type 8'
                           ' -s 192.168.11.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "ICMP Echo Request acceptance rule wasn't added")

        for ip in network_model.fixed_ips():
            if ip['version'] != 4:
                continue
            regex = re.compile('-A .* -j ACCEPT -p tcp -m multiport '
                               '--dports 80:81 -s %s' % ip['address'])
            self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                            "TCP port 80/81 acceptance rule wasn't added")
            regex = re.compile('-A .* -j ACCEPT -s %s' % ip['address'])
            self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                            "Protocol/port-less acceptance rule wasn't added")

        regex = re.compile('-A .* -j ACCEPT -p tcp '
                           '-m multiport --dports 80:81 -s 192.168.10.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "TCP port 80/81 acceptance rule wasn't added")
        db.instance_destroy(admin_ctxt, instance_ref['uuid'])

    def test_filters_for_instance_with_ip_v6(self):
        self.flags(use_ipv6=True)
        network_info = _fake_network_info(self.stubs, 1)
        rulesv4, rulesv6 = self.fw._filters_for_instance("fake", network_info)
        self.assertEquals(len(rulesv4), 2)
        self.assertEquals(len(rulesv6), 1)

    def test_filters_for_instance_without_ip_v6(self):
        self.flags(use_ipv6=False)
        network_info = _fake_network_info(self.stubs, 1)
        rulesv4, rulesv6 = self.fw._filters_for_instance("fake", network_info)
        self.assertEquals(len(rulesv4), 2)
        self.assertEquals(len(rulesv6), 0)

    def test_multinic_iptables(self):
        ipv4_rules_per_addr = 1
        ipv4_addr_per_network = 2
        ipv6_rules_per_addr = 1
        ipv6_addr_per_network = 1
        networks_count = 5
        instance_ref = self._create_instance_ref()
        network_info = _fake_network_info(self.stubs, networks_count,
                                                      ipv4_addr_per_network)
        ipv4_len = len(self.fw.iptables.ipv4['filter'].rules)
        ipv6_len = len(self.fw.iptables.ipv6['filter'].rules)
        inst_ipv4, inst_ipv6 = self.fw.instance_rules(instance_ref,
                                                      network_info)
        self.fw.prepare_instance_filter(instance_ref, network_info)
        ipv4 = self.fw.iptables.ipv4['filter'].rules
        ipv6 = self.fw.iptables.ipv6['filter'].rules
        ipv4_network_rules = len(ipv4) - len(inst_ipv4) - ipv4_len
        ipv6_network_rules = len(ipv6) - len(inst_ipv6) - ipv6_len
        self.assertEquals(ipv4_network_rules,
                  ipv4_rules_per_addr * ipv4_addr_per_network * networks_count)
        self.assertEquals(ipv6_network_rules,
                  ipv6_rules_per_addr * ipv6_addr_per_network * networks_count)

    def test_do_refresh_security_group_rules(self):
        instance_ref = self._create_instance_ref()
        self.mox.StubOutWithMock(self.fw,
                                 'add_filters_for_instance',
                                 use_mock_anything=True)
        self.fw.prepare_instance_filter(instance_ref, mox.IgnoreArg())
        self.fw.instances[instance_ref['id']] = instance_ref
        self.mox.ReplayAll()
        self.fw.do_refresh_security_group_rules("fake")

    def test_unfilter_instance_undefines_nwfilter(self):
        admin_ctxt = context.get_admin_context()

        fakefilter = NWFilterFakes()
        _xml_mock = fakefilter.filterDefineXMLMock
        self.fw.nwfilter._conn.nwfilterDefineXML = _xml_mock
        _lookup_name = fakefilter.nwfilterLookupByName
        self.fw.nwfilter._conn.nwfilterLookupByName = _lookup_name
        instance_ref = self._create_instance_ref()

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        self.fw.prepare_instance_filter(instance_ref, network_info)
        self.fw.apply_instance_filter(instance_ref, network_info)
        original_filter_count = len(fakefilter.filters)
        self.fw.unfilter_instance(instance_ref, network_info)

        # should undefine just the instance filter
        self.assertEqual(original_filter_count - len(fakefilter.filters), 1)

        db.instance_destroy(admin_ctxt, instance_ref['uuid'])

    def test_provider_firewall_rules(self):
        # setup basic instance data
        instance_ref = self._create_instance_ref()
        # FRAGILE: peeks at how the firewall names chains
        chain_name = 'inst-%s' % instance_ref['id']

        # create a firewall via setup_basic_filtering like libvirt_conn.spawn
        # should have a chain with 0 rules
        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        self.assertTrue('provider' in self.fw.iptables.ipv4['filter'].chains)
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(0, len(rules))

        admin_ctxt = context.get_admin_context()
        # add a rule and send the update message, check for 1 rule
        provider_fw0 = db.provider_fw_rule_create(admin_ctxt,
                                                  {'protocol': 'tcp',
                                                   'cidr': '10.99.99.99/32',
                                                   'from_port': 1,
                                                   'to_port': 65535})
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(1, len(rules))

        # Add another, refresh, and make sure number of rules goes to two
        provider_fw1 = db.provider_fw_rule_create(admin_ctxt,
                                                  {'protocol': 'udp',
                                                   'cidr': '10.99.99.99/32',
                                                   'from_port': 1,
                                                   'to_port': 65535})
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(2, len(rules))

        # create the instance filter and make sure it has a jump rule
        self.fw.prepare_instance_filter(instance_ref, network_info)
        self.fw.apply_instance_filter(instance_ref, network_info)
        inst_rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                           if rule.chain == chain_name]
        jump_rules = [rule for rule in inst_rules if '-j' in rule.rule]
        provjump_rules = []
        # IptablesTable doesn't make rules unique internally
        for rule in jump_rules:
            if 'provider' in rule.rule and rule not in provjump_rules:
                provjump_rules.append(rule)
        self.assertEqual(1, len(provjump_rules))

        # remove a rule from the db, cast to compute to refresh rule
        db.provider_fw_rule_destroy(admin_ctxt, provider_fw1['id'])
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(1, len(rules))


class NWFilterTestCase(test.TestCase):
    def setUp(self):
        super(NWFilterTestCase, self).setUp()

        class Mock(object):
            pass

        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id, self.project_id)

        self.fake_libvirt_connection = Mock()

        self.fw = firewall.NWFilterFirewall(
                                         lambda: self.fake_libvirt_connection)

    def test_cidr_rule_nwfilter_xml(self):
        cloud_controller = cloud.CloudController()
        cloud_controller.create_security_group(self.context,
                                               'testgroup',
                                               'test group description')
        cloud_controller.authorize_security_group_ingress(self.context,
                                                          'testgroup',
                                                          from_port='80',
                                                          to_port='81',
                                                          ip_protocol='tcp',
                                                          cidr_ip='0.0.0.0/0')

        security_group = db.security_group_get_by_name(self.context,
                                                       'fake',
                                                       'testgroup')
        self.teardown_security_group()

    def teardown_security_group(self):
        cloud_controller = cloud.CloudController()
        cloud_controller.delete_security_group(self.context, 'testgroup')

    def setup_and_return_security_group(self):
        cloud_controller = cloud.CloudController()
        cloud_controller.create_security_group(self.context,
                                               'testgroup',
                                               'test group description')
        cloud_controller.authorize_security_group_ingress(self.context,
                                                          'testgroup',
                                                          from_port='80',
                                                          to_port='81',
                                                          ip_protocol='tcp',
                                                          cidr_ip='0.0.0.0/0')

        return db.security_group_get_by_name(self.context, 'fake', 'testgroup')

    def _create_instance(self):
        return db.instance_create(self.context,
                                  {'user_id': 'fake',
                                   'project_id': 'fake',
                                   'instance_type_id': 1})

    def _create_instance_type(self, params=None):
        """Create a test instance"""
        if not params:
            params = {}

        context = self.context.elevated()
        inst = {}
        inst['name'] = 'm1.small'
        inst['memory_mb'] = '1024'
        inst['vcpus'] = '1'
        inst['root_gb'] = '10'
        inst['ephemeral_gb'] = '20'
        inst['flavorid'] = '1'
        inst['swap'] = '2048'
        inst['rxtx_factor'] = 1
        inst.update(params)
        return db.instance_type_create(context, inst)['id']

    def test_creates_base_rule_first(self):
        # These come pre-defined by libvirt
        self.defined_filters = ['no-mac-spoofing',
                                'no-ip-spoofing',
                                'no-arp-spoofing',
                                'allow-dhcp-server']

        self.recursive_depends = {}
        for f in self.defined_filters:
            self.recursive_depends[f] = []

        def _filterDefineXMLMock(xml):
            dom = minidom.parseString(xml)
            name = dom.firstChild.getAttribute('name')
            self.recursive_depends[name] = []
            for f in dom.getElementsByTagName('filterref'):
                ref = f.getAttribute('filter')
                self.assertTrue(ref in self.defined_filters,
                                ('%s referenced filter that does ' +
                                'not yet exist: %s') % (name, ref))
                dependencies = [ref] + self.recursive_depends[ref]
                self.recursive_depends[name] += dependencies

            self.defined_filters.append(name)
            return True

        self.fake_libvirt_connection.nwfilterDefineXML = _filterDefineXMLMock

        instance_ref = self._create_instance()
        inst_id = instance_ref['id']
        inst_uuid = instance_ref['uuid']

        def _ensure_all_called(mac, allow_dhcp):
            instance_filter = 'nova-instance-%s-%s' % (instance_ref['name'],
                                                   mac.translate(None, ':'))
            requiredlist = ['no-arp-spoofing', 'no-ip-spoofing',
                             'no-mac-spoofing']
            if allow_dhcp:
                requiredlist.append('allow-dhcp-server')
            for required in requiredlist:
                self.assertTrue(required in
                                self.recursive_depends[instance_filter],
                                "Instance's filter does not include %s" %
                                required)

        self.security_group = self.setup_and_return_security_group()

        db.instance_add_security_group(self.context, inst_uuid,
                                       self.security_group.id)
        instance = db.instance_get(self.context, inst_id)

        network_info = _fake_network_info(self.stubs, 1)
        # since there is one (network_info) there is one vif
        # pass this vif's mac to _ensure_all_called()
        # to set the instance_filter properly
        mac = network_info[0][1]['mac']

        self.fw.setup_basic_filtering(instance, network_info)
        allow_dhcp = False
        for (network, mapping) in network_info:
            if mapping['dhcp_server']:
                allow_dhcp = True
                break
        _ensure_all_called(mac, allow_dhcp)
        db.instance_remove_security_group(self.context, inst_uuid,
                                          self.security_group.id)
        self.teardown_security_group()
        db.instance_destroy(context.get_admin_context(), instance_ref['uuid'])

    def test_unfilter_instance_undefines_nwfilters(self):
        admin_ctxt = context.get_admin_context()

        fakefilter = NWFilterFakes()
        self.fw._conn.nwfilterDefineXML = fakefilter.filterDefineXMLMock
        self.fw._conn.nwfilterLookupByName = fakefilter.nwfilterLookupByName

        instance_ref = self._create_instance()
        inst_id = instance_ref['id']
        inst_uuid = instance_ref['uuid']

        self.security_group = self.setup_and_return_security_group()

        db.instance_add_security_group(self.context, inst_uuid,
                                       self.security_group.id)

        instance = db.instance_get(self.context, inst_id)

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance, network_info)
        original_filter_count = len(fakefilter.filters)
        self.fw.unfilter_instance(instance, network_info)
        self.assertEqual(original_filter_count - len(fakefilter.filters), 1)

        db.instance_destroy(admin_ctxt, instance_ref['uuid'])


class LibvirtUtilsTestCase(test.TestCase):
    def test_get_iscsi_initiator(self):
        self.mox.StubOutWithMock(utils, 'execute')
        initiator = 'fake.initiator.iqn'
        rval = ("junk\nInitiatorName=%s\njunk\n" % initiator, None)
        utils.execute('cat', '/etc/iscsi/initiatorname.iscsi',
                      run_as_root=True).AndReturn(rval)
        # Start test
        self.mox.ReplayAll()
        result = libvirt_utils.get_iscsi_initiator()
        self.assertEqual(initiator, result)

    def test_create_image(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('qemu-img', 'create', '-f', 'raw',
                      '/some/path', '10G')
        utils.execute('qemu-img', 'create', '-f', 'qcow2',
                      '/some/stuff', '1234567891234')
        # Start test
        self.mox.ReplayAll()
        libvirt_utils.create_image('raw', '/some/path', '10G')
        libvirt_utils.create_image('qcow2', '/some/stuff', '1234567891234')

    def test_create_cow_image(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('qemu-img', 'create', '-f', 'qcow2',
                      '-o', 'backing_file=/some/path',
                      '/the/new/cow')
        # Start test
        self.mox.ReplayAll()
        libvirt_utils.create_cow_image('/some/path', '/the/new/cow')

    def test_get_disk_size(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('qemu-img',
                      'info',
                      '/some/path').AndReturn(('''image: 00000001
file format: raw
virtual size: 4.4M (4592640 bytes)
disk size: 4.4M''', ''))

        # Start test
        self.mox.ReplayAll()
        self.assertEquals(libvirt_utils.get_disk_size('/some/path'), 4592640)

    def test_copy_image(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            src_fd, src_path = tempfile.mkstemp()
            try:
                with os.fdopen(src_fd, 'w') as fp:
                    fp.write('canary')

                libvirt_utils.copy_image(src_path, dst_path)
                with open(dst_path, 'r') as fp:
                    self.assertEquals(fp.read(), 'canary')
            finally:
                os.unlink(src_path)
        finally:
            os.unlink(dst_path)

    def test_mkfs(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('mkfs', '-t', 'ext4', '-F', '/my/block/dev')
        utils.execute('mkswap', '/my/swap/block/dev')
        self.mox.ReplayAll()

        libvirt_utils.mkfs('ext4', '/my/block/dev')
        libvirt_utils.mkfs('swap', '/my/swap/block/dev')

    def test_ensure_tree(self):
        with utils.tempdir() as tmpdir:
            testdir = '%s/foo/bar/baz' % (tmpdir,)
            libvirt_utils.ensure_tree(testdir)
            self.assertTrue(os.path.isdir(testdir))

    def test_write_to_file(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            libvirt_utils.write_to_file(dst_path, 'hello')
            with open(dst_path, 'r') as fp:
                self.assertEquals(fp.read(), 'hello')
        finally:
            os.unlink(dst_path)

    def test_write_to_file_with_umask(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)
            os.unlink(dst_path)

            libvirt_utils.write_to_file(dst_path, 'hello', umask=0277)
            with open(dst_path, 'r') as fp:
                self.assertEquals(fp.read(), 'hello')
            mode = os.stat(dst_path).st_mode
            self.assertEquals(mode & 0277, 0)
        finally:
            os.unlink(dst_path)

    def test_chown(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('chown', 'soren', '/some/path', run_as_root=True)
        self.mox.ReplayAll()
        libvirt_utils.chown('/some/path', 'soren')

    def test_extract_snapshot(self):
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('qemu-img', 'convert', '-f', 'qcow2', '-O', 'raw',
                      '-s', 'snap1', '/path/to/disk/image', '/extracted/snap')

        # Start test
        self.mox.ReplayAll()
        libvirt_utils.extract_snapshot('/path/to/disk/image', 'qcow2',
                                       'snap1', '/extracted/snap', 'raw')

    def test_load_file(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            # We have a test for write_to_file. If that is sound, this suffices
            libvirt_utils.write_to_file(dst_path, 'hello')
            self.assertEquals(libvirt_utils.load_file(dst_path), 'hello')
        finally:
            os.unlink(dst_path)

    def test_file_open(self):
        dst_fd, dst_path = tempfile.mkstemp()
        try:
            os.close(dst_fd)

            # We have a test for write_to_file. If that is sound, this suffices
            libvirt_utils.write_to_file(dst_path, 'hello')
            with libvirt_utils.file_open(dst_path, 'r') as fp:
                self.assertEquals(fp.read(), 'hello')
        finally:
            os.unlink(dst_path)

    def test_get_fs_info(self):

        class FakeStatResult(object):

            def __init__(self):
                self.f_bsize = 4096
                self.f_frsize = 4096
                self.f_blocks = 2000
                self.f_bfree = 1000
                self.f_bavail = 900
                self.f_files = 2000
                self.f_ffree = 1000
                self.f_favail = 900
                self.f_flag = 4096
                self.f_namemax = 255

        self.path = None

        def fake_statvfs(path):
            self.path = path
            return FakeStatResult()

        self.stubs.Set(os, 'statvfs', fake_statvfs)

        fs_info = libvirt_utils.get_fs_info('/some/file/path')
        self.assertEquals('/some/file/path', self.path)
        self.assertEquals(8192000, fs_info['total'])
        self.assertEquals(3686400, fs_info['free'])
        self.assertEquals(4096000, fs_info['used'])

    def test_fetch_image(self):
        self.mox.StubOutWithMock(images, 'fetch_to_raw')

        context = 'opaque context'
        target = '/tmp/targetfile'
        image_id = '4'
        user_id = 'fake'
        project_id = 'fake'
        images.fetch_to_raw(context, image_id, target, user_id, project_id)

        self.mox.ReplayAll()
        libvirt_utils.fetch_image(context, target, image_id,
                                  user_id, project_id)

    def test_get_disk_backing_file(self):
        with_actual_path = False

        def fake_execute(*args, **kwargs):
            if with_actual_path:
                return ("some\n"
                        "output\n"
                        "backing file: /foo/bar/baz (actual path: /a/b/c)\n"
                        "...\n"), ''
            else:
                return ("some\n"
                        "output\n"
                        "backing file: /foo/bar/baz\n"
                        "...\n"), ''

        self.stubs.Set(utils, 'execute', fake_execute)

        out = libvirt_utils.get_disk_backing_file('')
        self.assertEqual(out, 'baz')
        with_actual_path = True
        out = libvirt_utils.get_disk_backing_file('')
        self.assertEqual(out, 'c')


class LibvirtDriverTestCase(test.TestCase):
    """Test for nova.virt.libvirt.libvirt_driver.LibvirtDriver."""
    def setUp(self):
        super(LibvirtDriverTestCase, self).setUp()
        self.libvirtconnection = libvirt_driver.LibvirtDriver(read_only=True)

    def _create_instance(self, params=None):
        """Create a test instance"""
        if not params:
            params = {}

        inst = {}
        inst['image_ref'] = '1'
        inst['reservation_id'] = 'r-fakeres'
        inst['launch_time'] = '10'
        inst['user_id'] = 'fake'
        inst['project_id'] = 'fake'
        type_id = instance_types.get_instance_type_by_name('m1.tiny')['id']
        inst['instance_type_id'] = type_id
        inst['ami_launch_index'] = 0
        inst['host'] = 'host1'
        inst['root_gb'] = 10
        inst['ephemeral_gb'] = 20
        inst['config_drive'] = 1
        inst['kernel_id'] = 2
        inst['ramdisk_id'] = 3
        inst['config_drive_id'] = 1
        inst['key_data'] = 'ABCDEFG'

        inst.update(params)
        return db.instance_create(context.get_admin_context(), inst)

    def test_migrate_disk_and_power_off_exception(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .migrate_disk_and_power_off. """

        self.counter = 0

        def fake_get_instance_disk_info(instance):
            return '[]'

        def fake_destroy(instance):
            pass

        def fake_get_host_ip_addr():
            return '10.0.0.1'

        def fake_execute(*args, **kwargs):
            self.counter += 1
            if self.counter == 1:
                assert False, "intentional failure"

        def fake_os_path_exists(path):
            return True

        self.stubs.Set(self.libvirtconnection, 'get_instance_disk_info',
                       fake_get_instance_disk_info)
        self.stubs.Set(self.libvirtconnection, '_destroy', fake_destroy)
        self.stubs.Set(self.libvirtconnection, 'get_host_ip_addr',
                       fake_get_host_ip_addr)
        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(os.path, 'exists', fake_os_path_exists)

        ins_ref = self._create_instance()

        self.assertRaises(AssertionError,
                          self.libvirtconnection.migrate_disk_and_power_off,
                          None, ins_ref, '10.0.0.2', None, None)

    def test_migrate_disk_and_power_off(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .migrate_disk_and_power_off. """

        disk_info = [{'type': 'qcow2', 'path': '/test/disk',
                      'virt_disk_size': '10737418240',
                      'backing_file': '/base/disk',
                      'disk_size':'83886080'},
                     {'type': 'raw', 'path': '/test/disk.local',
                      'virt_disk_size': '10737418240',
                      'backing_file': '/base/disk.local',
                      'disk_size':'83886080'}]
        disk_info_text = jsonutils.dumps(disk_info)

        def fake_get_instance_disk_info(instance):
            return disk_info_text

        def fake_destroy(instance):
            pass

        def fake_get_host_ip_addr():
            return '10.0.0.1'

        def fake_execute(*args, **kwargs):
            pass

        self.stubs.Set(self.libvirtconnection, 'get_instance_disk_info',
                       fake_get_instance_disk_info)
        self.stubs.Set(self.libvirtconnection, '_destroy', fake_destroy)
        self.stubs.Set(self.libvirtconnection, 'get_host_ip_addr',
                       fake_get_host_ip_addr)
        self.stubs.Set(utils, 'execute', fake_execute)

        ins_ref = self._create_instance()
        """ dest is different host case """
        out = self.libvirtconnection.migrate_disk_and_power_off(
               None, ins_ref, '10.0.0.2', None, None)
        self.assertEquals(out, disk_info_text)

        """ dest is same host case """
        out = self.libvirtconnection.migrate_disk_and_power_off(
               None, ins_ref, '10.0.0.1', None, None)
        self.assertEquals(out, disk_info_text)

    def test_wait_for_running(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        ._wait_for_running. """

        def fake_get_info(instance):
            if instance['name'] == "not_found":
                raise exception.NotFound
            elif instance['name'] == "running":
                return {'state': power_state.RUNNING}
            else:
                return {'state': power_state.SHUTDOWN}

        self.stubs.Set(self.libvirtconnection, 'get_info',
                       fake_get_info)

        """ instance not found case """
        self.assertRaises(utils.LoopingCallDone,
                self.libvirtconnection._wait_for_running,
                    {'name': 'not_found',
                     'uuid': 'not_found_uuid'})

        """ instance is running case """
        self.assertRaises(utils.LoopingCallDone,
                self.libvirtconnection._wait_for_running,
                    {'name': 'running',
                     'uuid': 'running_uuid'})

        """ else case """
        self.libvirtconnection._wait_for_running({'name': 'else',
                                                  'uuid': 'other_uuid'})

    def test_finish_migration(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .finish_migration. """

        disk_info = [{'type': 'qcow2', 'path': '/test/disk',
                      'local_gb': 10, 'backing_file': '/base/disk'},
                     {'type': 'raw', 'path': '/test/disk.local',
                      'local_gb': 10, 'backing_file': '/base/disk.local'}]
        disk_info_text = jsonutils.dumps(disk_info)

        def fake_extend(path, size):
            pass

        def fake_to_xml(instance, network_info):
            return ""

        def fake_plug_vifs(instance, network_info):
            pass

        def fake_create_image(context, inst, libvirt_xml, suffix='',
                      disk_images=None, network_info=None,
                      block_device_info=None):
            pass

        def fake_create_domain(xml):
            return None

        def fake_enable_hairpin(instance):
            pass

        def fake_execute(*args, **kwargs):
            pass

        def fake_get_info(instance):
            return {'state': power_state.RUNNING}

        self.flags(use_cow_images=True)
        self.stubs.Set(libvirt_driver.disk, 'extend', fake_extend)
        self.stubs.Set(self.libvirtconnection, 'to_xml', fake_to_xml)
        self.stubs.Set(self.libvirtconnection, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(self.libvirtconnection, '_create_image',
                       fake_create_image)
        self.stubs.Set(self.libvirtconnection, '_create_domain',
                       fake_create_domain)
        self.stubs.Set(self.libvirtconnection, '_enable_hairpin',
                       fake_enable_hairpin)
        self.stubs.Set(utils, 'execute', fake_execute)
        fw = base_firewall.NoopFirewallDriver()
        self.stubs.Set(self.libvirtconnection, 'firewall_driver', fw)
        self.stubs.Set(self.libvirtconnection, 'get_info',
                       fake_get_info)

        ins_ref = self._create_instance()

        self.libvirtconnection.finish_migration(
                      context.get_admin_context(), None, ins_ref,
                      disk_info_text, None, None, None)

    def test_finish_revert_migration(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .finish_revert_migration. """

        def fake_execute(*args, **kwargs):
            pass

        def fake_plug_vifs(instance, network_info):
            pass

        def fake_create_domain(xml):
            return None

        def fake_enable_hairpin(instance):
            pass

        def fake_get_info(instance):
            return {'state': power_state.RUNNING}

        self.stubs.Set(self.libvirtconnection, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(utils, 'execute', fake_execute)
        fw = base_firewall.NoopFirewallDriver()
        self.stubs.Set(self.libvirtconnection, 'firewall_driver', fw)
        self.stubs.Set(self.libvirtconnection, '_create_domain',
                       fake_create_domain)
        self.stubs.Set(self.libvirtconnection, '_enable_hairpin',
                       fake_enable_hairpin)
        self.stubs.Set(self.libvirtconnection, 'get_info',
                       fake_get_info)

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)
            ins_ref = self._create_instance()
            os.mkdir(os.path.join(tmpdir, ins_ref['name']))
            libvirt_xml_path = os.path.join(tmpdir,
                                            ins_ref['name'],
                                            'libvirt.xml')
            f = open(libvirt_xml_path, 'w')
            f.close()

            self.libvirtconnection.finish_revert_migration(ins_ref, None)


class LibvirtNonblockingTestCase(test.TestCase):
    """Test libvirt_nonblocking option"""

    def setUp(self):
        super(LibvirtNonblockingTestCase, self).setUp()
        self.flags(libvirt_nonblocking=True, libvirt_uri="test:///default")

    def tearDown(self):
        super(LibvirtNonblockingTestCase, self).tearDown()

    def test_connection_to_primitive(self):
        """Test bug 962840"""
        import nova.virt.libvirt.driver as libvirt_driver
        connection = libvirt_driver.LibvirtDriver('')
        jsonutils.to_primitive(connection._conn, convert_instances=True)
