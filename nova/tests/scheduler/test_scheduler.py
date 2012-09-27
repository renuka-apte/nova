# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
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
"""
Tests For Scheduler
"""

import mox

from nova.compute import api as compute_api
from nova.compute import power_state
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova import context
from nova import db
from nova import exception
from nova import flags
from nova.openstack.common import jsonutils
from nova.openstack.common import rpc
from nova.openstack.common import timeutils
from nova.scheduler import driver
from nova.scheduler import manager
from nova import test
from nova.tests.scheduler import fakes
from nova import utils

FLAGS = flags.FLAGS


class SchedulerManagerTestCase(test.TestCase):
    """Test case for scheduler manager"""

    manager_cls = manager.SchedulerManager
    driver_cls = driver.Scheduler
    driver_cls_name = 'nova.scheduler.driver.Scheduler'

    def setUp(self):
        super(SchedulerManagerTestCase, self).setUp()
        self.flags(scheduler_driver=self.driver_cls_name)
        self.stubs.Set(compute_api, 'API', fakes.FakeComputeAPI)
        self.manager = self.manager_cls()
        self.context = context.RequestContext('fake_user', 'fake_project')
        self.topic = 'fake_topic'
        self.fake_args = (1, 2, 3)
        self.fake_kwargs = {'cat': 'meow', 'dog': 'woof'}

    def test_1_correct_init(self):
        # Correct scheduler driver
        manager = self.manager
        self.assertTrue(isinstance(manager.driver, self.driver_cls))

    def test_update_service_capabilities(self):
        service_name = 'fake_service'
        host = 'fake_host'

        self.mox.StubOutWithMock(self.manager.driver,
                'update_service_capabilities')

        # Test no capabilities passes empty dictionary
        self.manager.driver.update_service_capabilities(service_name,
                host, {})
        self.mox.ReplayAll()
        result = self.manager.update_service_capabilities(self.context,
                service_name=service_name, host=host, capabilities={})
        self.mox.VerifyAll()

        self.mox.ResetAll()
        # Test capabilities passes correctly
        capabilities = {'fake_capability': 'fake_value'}
        self.manager.driver.update_service_capabilities(
                service_name, host, capabilities)
        self.mox.ReplayAll()
        result = self.manager.update_service_capabilities(self.context,
                service_name=service_name, host=host,
                capabilities=capabilities)

    def test_show_host_resources(self):
        host = 'fake_host'

        computes = [{'host': host,
                     'compute_node': [{'vcpus': 4,
                                      'vcpus_used': 2,
                                      'memory_mb': 1024,
                                      'memory_mb_used': 512,
                                      'local_gb': 1024,
                                      'local_gb_used': 512}]}]
        instances = [{'project_id': 'project1',
                      'vcpus': 1,
                      'memory_mb': 128,
                      'root_gb': 128,
                      'ephemeral_gb': 0},
                     {'project_id': 'project1',
                      'vcpus': 2,
                      'memory_mb': 256,
                      'root_gb': 384,
                      'ephemeral_gb': 0},
                     {'project_id': 'project2',
                      'vcpus': 2,
                      'memory_mb': 256,
                      'root_gb': 256,
                      'ephemeral_gb': 0}]

        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')
        self.mox.StubOutWithMock(db, 'instance_get_all_by_host')

        db.service_get_all_compute_by_host(self.context, host).AndReturn(
                computes)
        db.instance_get_all_by_host(self.context, host).AndReturn(instances)

        self.mox.ReplayAll()
        result = self.manager.show_host_resources(self.context, host)
        expected = {'usage': {'project1': {'memory_mb': 384,
                                           'vcpus': 3,
                                           'root_gb': 512,
                                           'ephemeral_gb': 0},
                              'project2': {'memory_mb': 256,
                                           'vcpus': 2,
                                           'root_gb': 256,
                                           'ephemeral_gb': 0}},
                    'resource': {'vcpus': 4,
                                 'vcpus_used': 2,
                                 'local_gb': 1024,
                                 'local_gb_used': 512,
                                 'memory_mb': 1024,
                                 'memory_mb_used': 512}}
        self.assertDictMatch(result, expected)

    def _mox_schedule_method_helper(self, method_name):
        # Make sure the method exists that we're going to test call
        def stub_method(*args, **kwargs):
            pass

        setattr(self.manager.driver, method_name, stub_method)

        self.mox.StubOutWithMock(self.manager.driver,
                method_name)

    def test_run_instance_exception_puts_instance_in_error_state(self):
        fake_instance_uuid = 'fake-instance-id'
        inst = {"vm_state": "", "task_state": ""}

        self._mox_schedule_method_helper('schedule_run_instance')
        self.mox.StubOutWithMock(compute_utils, 'add_instance_fault_from_exc')
        self.mox.StubOutWithMock(db, 'instance_update_and_get_original')

        request_spec = {'instance_properties':
                {'uuid': fake_instance_uuid}}

        self.manager.driver.schedule_run_instance(self.context,
                request_spec, None, None, None, None, {}).AndRaise(
                        exception.NoValidHost(reason=""))
        db.instance_update_and_get_original(self.context, fake_instance_uuid,
                {"vm_state": vm_states.ERROR,
                 "task_state": None}).AndReturn((inst, inst))
        compute_utils.add_instance_fault_from_exc(self.context,
                fake_instance_uuid, mox.IsA(exception.NoValidHost),
                mox.IgnoreArg())

        self.mox.ReplayAll()
        self.manager.run_instance(self.context, request_spec,
                None, None, None, None, {})

    def test_create_volume_no_valid_host_puts_volume_in_error(self):
        self._mox_schedule_method_helper('schedule_create_volume')
        self.mox.StubOutWithMock(db, 'volume_update')

        self.manager.driver.schedule_create_volume(self.context, '1', '2',
                None).AndRaise(exception.NoValidHost(reason=''))
        db.volume_update(self.context, '1', {'status': 'error'})

        self.mox.ReplayAll()
        self.assertRaises(exception.NoValidHost, self.manager.create_volume,
                          self.context, '1', '2')

    def test_prep_resize_no_valid_host_back_in_active_state(self):
        fake_instance_uuid = 'fake-instance-id'
        inst = {"vm_state": "", "task_state": ""}

        self._mox_schedule_method_helper('schedule_prep_resize')

        self.mox.StubOutWithMock(compute_utils, 'add_instance_fault_from_exc')
        self.mox.StubOutWithMock(db, 'instance_update_and_get_original')

        request_spec = {'instance_type': 'fake_type',
                        'instance_uuids': [fake_instance_uuid],
                        'instance_properties': {'uuid': fake_instance_uuid}}
        kwargs = {
                'context': self.context,
                'image': 'fake_image',
                'request_spec': request_spec,
                'filter_properties': 'fake_props',
                'instance': 'fake_instance',
                'instance_type': 'fake_type',
                'reservations': list('fake_res'),
        }
        self.manager.driver.schedule_prep_resize(**kwargs).AndRaise(
                exception.NoValidHost(reason=""))
        db.instance_update_and_get_original(self.context, fake_instance_uuid,
                {"vm_state": vm_states.ACTIVE, "task_state": None}).AndReturn(
                        (inst, inst))
        compute_utils.add_instance_fault_from_exc(self.context,
                fake_instance_uuid, mox.IsA(exception.NoValidHost),
                mox.IgnoreArg())

        self.mox.ReplayAll()
        self.manager.prep_resize(**kwargs)

    def test_prep_resize_exception_host_in_error_state_and_raise(self):
        fake_instance_uuid = 'fake-instance-id'

        self._mox_schedule_method_helper('schedule_prep_resize')

        self.mox.StubOutWithMock(compute_utils, 'add_instance_fault_from_exc')
        self.mox.StubOutWithMock(db, 'instance_update_and_get_original')

        request_spec = {'instance_properties':
                {'uuid': fake_instance_uuid}}
        kwargs = {
                'context': self.context,
                'image': 'fake_image',
                'request_spec': request_spec,
                'filter_properties': 'fake_props',
                'instance': 'fake_instance',
                'instance_type': 'fake_type',
                'reservations': list('fake_res'),
        }

        self.manager.driver.schedule_prep_resize(**kwargs).AndRaise(
                test.TestingException('something happened'))

        inst = {
            "vm_state": "",
            "task_state": "",
        }
        db.instance_update_and_get_original(self.context, fake_instance_uuid,
                {"vm_state": vm_states.ERROR,
                 "task_state": None}).AndReturn((inst, inst))
        compute_utils.add_instance_fault_from_exc(self.context,
                fake_instance_uuid, mox.IsA(test.TestingException),
                mox.IgnoreArg())

        self.mox.ReplayAll()

        self.assertRaises(test.TestingException, self.manager.prep_resize,
                          **kwargs)


class SchedulerTestCase(test.TestCase):
    """Test case for base scheduler driver class"""

    # So we can subclass this test and re-use tests if we need.
    driver_cls = driver.Scheduler

    def setUp(self):
        super(SchedulerTestCase, self).setUp()
        self.stubs.Set(compute_api, 'API', fakes.FakeComputeAPI)
        self.driver = self.driver_cls()
        self.context = context.RequestContext('fake_user', 'fake_project')
        self.topic = 'fake_topic'

    def test_update_service_capabilities(self):
        service_name = 'fake_service'
        host = 'fake_host'

        self.mox.StubOutWithMock(self.driver.host_manager,
                'update_service_capabilities')

        capabilities = {'fake_capability': 'fake_value'}
        self.driver.host_manager.update_service_capabilities(
                service_name, host, capabilities)
        self.mox.ReplayAll()
        result = self.driver.update_service_capabilities(service_name,
                host, capabilities)

    def test_hosts_up(self):
        service1 = {'host': 'host1'}
        service2 = {'host': 'host2'}
        services = [service1, service2]

        self.mox.StubOutWithMock(db, 'service_get_all_by_topic')
        self.mox.StubOutWithMock(utils, 'service_is_up')

        db.service_get_all_by_topic(self.context,
                self.topic).AndReturn(services)
        utils.service_is_up(service1).AndReturn(False)
        utils.service_is_up(service2).AndReturn(True)

        self.mox.ReplayAll()
        result = self.driver.hosts_up(self.context, self.topic)
        self.assertEqual(result, ['host2'])

    def _live_migration_instance(self):
        volume1 = {'id': 31338}
        volume2 = {'id': 31339}
        return {'id': 31337,
                'uuid': 'fake_uuid',
                'name': 'fake-instance',
                'host': 'fake_host1',
                'volumes': [volume1, volume2],
                'power_state': power_state.RUNNING,
                'memory_mb': 1024,
                'root_gb': 1024,
                'ephemeral_gb': 0,
                'vm_state': '',
                'task_state': ''}

    def test_live_migration_basic(self):
        """Test basic schedule_live_migration functionality"""
        self.mox.StubOutWithMock(self.driver, '_live_migration_src_check')
        self.mox.StubOutWithMock(self.driver, '_live_migration_dest_check')
        self.mox.StubOutWithMock(self.driver, '_live_migration_common_check')
        self.mox.StubOutWithMock(self.driver.compute_rpcapi,
                                 'check_can_live_migrate_destination')
        self.mox.StubOutWithMock(self.driver.compute_rpcapi,
                                 'live_migration')

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = jsonutils.to_primitive(self._live_migration_instance())
        instance_id = instance['id']
        instance_uuid = instance['uuid']

        self.driver._live_migration_src_check(self.context, instance)
        self.driver._live_migration_dest_check(self.context, instance, dest)
        self.driver._live_migration_common_check(self.context, instance,
                                                 dest)
        self.driver.compute_rpcapi.check_can_live_migrate_destination(
               self.context, instance, dest, block_migration,
               disk_over_commit).AndReturn({})
        self.driver.compute_rpcapi.live_migration(self.context,
                host=instance['host'], instance=instance, dest=dest,
                block_migration=block_migration, migrate_data={})

        self.mox.ReplayAll()
        self.driver.schedule_live_migration(self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)

    def test_live_migration_all_checks_pass(self):
        """Test live migration when all checks pass."""

        self.mox.StubOutWithMock(utils, 'service_is_up')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')
        self.mox.StubOutWithMock(db, 'instance_get_all_by_host')
        self.mox.StubOutWithMock(rpc, 'call')
        self.mox.StubOutWithMock(rpc, 'cast')
        self.mox.StubOutWithMock(self.driver.compute_rpcapi,
                                 'live_migration')

        dest = 'fake_host2'
        block_migration = True
        disk_over_commit = True
        instance = jsonutils.to_primitive(self._live_migration_instance())
        instance_id = instance['id']
        instance_uuid = instance['uuid']

        # Source checks
        db.service_get_all_compute_by_host(self.context,
                instance['host']).AndReturn(['fake_service2'])
        utils.service_is_up('fake_service2').AndReturn(True)

        # Destination checks (compute is up, enough memory, disk)
        db.service_get_all_compute_by_host(self.context,
                dest).AndReturn(['fake_service3'])
        utils.service_is_up('fake_service3').AndReturn(True)
        # assert_compute_node_has_enough_memory()
        db.service_get_all_compute_by_host(self.context, dest).AndReturn(
                [{'compute_node': [{'memory_mb': 2048,
                                    'hypervisor_version': 1}]}])
        db.instance_get_all_by_host(self.context, dest).AndReturn(
                [dict(memory_mb=256), dict(memory_mb=512)])

        # Common checks (same hypervisor, etc)
        db.service_get_all_compute_by_host(self.context, dest).AndReturn(
                [{'compute_node': [{'hypervisor_type': 'xen',
                                    'hypervisor_version': 1}]}])
        db.service_get_all_compute_by_host(self.context,
            instance['host']).AndReturn(
                    [{'compute_node': [{'hypervisor_type': 'xen',
                                        'hypervisor_version': 1,
                                        'cpu_info': 'fake_cpu_info'}]}])

        rpc.call(self.context, "compute.fake_host2",
                   {"method": 'check_can_live_migrate_destination',
                    "args": {'instance': instance,
                             'block_migration': block_migration,
                             'disk_over_commit': disk_over_commit},
                    "version": compute_rpcapi.ComputeAPI.BASE_RPC_API_VERSION},
                 None).AndReturn({})

        self.driver.compute_rpcapi.live_migration(self.context,
                host=instance['host'], instance=instance, dest=dest,
                block_migration=block_migration, migrate_data={})

        self.mox.ReplayAll()
        result = self.driver.schedule_live_migration(self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)
        self.assertEqual(result, None)

    def test_live_migration_instance_not_running(self):
        """The instance given by instance_id is not running."""

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()
        instance['power_state'] = power_state.NOSTATE

        self.assertRaises(exception.InstanceNotRunning,
            self.driver.schedule_live_migration, self.context,
                    instance=instance, dest=dest,
                    block_migration=block_migration,
                    disk_over_commit=disk_over_commit)

    def test_live_migration_compute_src_not_exist(self):
        """Raise exception when src compute node is does not exist."""

        self.mox.StubOutWithMock(utils, 'service_is_up')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()

        # Compute down
        db.service_get_all_compute_by_host(self.context,
                instance['host']).AndRaise(
                                       exception.NotFound())

        self.mox.ReplayAll()
        self.assertRaises(exception.ComputeServiceUnavailable,
                self.driver.schedule_live_migration, self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)

    def test_live_migration_compute_src_not_alive(self):
        """Raise exception when src compute node is not alive."""

        self.mox.StubOutWithMock(utils, 'service_is_up')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()

        # Compute down
        db.service_get_all_compute_by_host(self.context,
                instance['host']).AndReturn(['fake_service2'])
        utils.service_is_up('fake_service2').AndReturn(False)

        self.mox.ReplayAll()
        self.assertRaises(exception.ComputeServiceUnavailable,
                self.driver.schedule_live_migration, self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)

    def test_live_migration_compute_dest_not_alive(self):
        """Raise exception when dest compute node is not alive."""

        self.mox.StubOutWithMock(self.driver, '_live_migration_src_check')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')
        self.mox.StubOutWithMock(utils, 'service_is_up')

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()

        self.driver._live_migration_src_check(self.context, instance)
        db.service_get_all_compute_by_host(self.context,
                dest).AndReturn(['fake_service3'])
        # Compute is down
        utils.service_is_up('fake_service3').AndReturn(False)

        self.mox.ReplayAll()
        self.assertRaises(exception.ComputeServiceUnavailable,
                self.driver.schedule_live_migration, self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)

    def test_live_migration_dest_check_service_same_host(self):
        """Confirms exception raises in case dest and src is same host."""

        self.mox.StubOutWithMock(self.driver, '_live_migration_src_check')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')
        self.mox.StubOutWithMock(utils, 'service_is_up')

        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()
        # make dest same as src
        dest = instance['host']

        self.driver._live_migration_src_check(self.context, instance)
        db.service_get_all_compute_by_host(self.context,
                dest).AndReturn(['fake_service3'])
        utils.service_is_up('fake_service3').AndReturn(True)

        self.mox.ReplayAll()
        self.assertRaises(exception.UnableToMigrateToSelf,
                self.driver.schedule_live_migration, self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=False)

    def test_live_migration_dest_check_service_lack_memory(self):
        """Confirms exception raises when dest doesn't have enough memory."""

        self.mox.StubOutWithMock(self.driver, '_live_migration_src_check')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')
        self.mox.StubOutWithMock(utils, 'service_is_up')
        self.mox.StubOutWithMock(self.driver, '_get_compute_info')
        self.mox.StubOutWithMock(db, 'instance_get_all_by_host')

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()

        self.driver._live_migration_src_check(self.context, instance)
        db.service_get_all_compute_by_host(self.context,
                dest).AndReturn(['fake_service3'])
        utils.service_is_up('fake_service3').AndReturn(True)

        self.driver._get_compute_info(self.context, dest).AndReturn(
                                                       {'memory_mb': 2048})
        db.instance_get_all_by_host(self.context, dest).AndReturn(
                [dict(memory_mb=1024), dict(memory_mb=512)])

        self.mox.ReplayAll()
        self.assertRaises(exception.MigrationError,
                self.driver.schedule_live_migration, self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)

    def test_live_migration_different_hypervisor_type_raises(self):
        """Confirm live_migration to hypervisor of different type raises"""
        self.mox.StubOutWithMock(self.driver, '_live_migration_src_check')
        self.mox.StubOutWithMock(self.driver, '_live_migration_dest_check')
        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'call')
        self.mox.StubOutWithMock(rpc, 'cast')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()

        self.driver._live_migration_src_check(self.context, instance)
        self.driver._live_migration_dest_check(self.context, instance, dest)

        db.service_get_all_compute_by_host(self.context, dest).AndReturn(
                [{'compute_node': [{'hypervisor_type': 'xen',
                                    'hypervisor_version': 1}]}])
        db.service_get_all_compute_by_host(self.context,
            instance['host']).AndReturn(
                    [{'compute_node': [{'hypervisor_type': 'not-xen',
                                        'hypervisor_version': 1}]}])

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidHypervisorType,
                self.driver.schedule_live_migration, self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)

    def test_live_migration_dest_hypervisor_version_older_raises(self):
        """Confirm live migration to older hypervisor raises"""
        self.mox.StubOutWithMock(self.driver, '_live_migration_src_check')
        self.mox.StubOutWithMock(self.driver, '_live_migration_dest_check')
        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'call')
        self.mox.StubOutWithMock(rpc, 'cast')
        self.mox.StubOutWithMock(db, 'service_get_all_compute_by_host')

        dest = 'fake_host2'
        block_migration = False
        disk_over_commit = False
        instance = self._live_migration_instance()

        self.driver._live_migration_src_check(self.context, instance)
        self.driver._live_migration_dest_check(self.context, instance, dest)

        db.service_get_all_compute_by_host(self.context, dest).AndReturn(
                [{'compute_node': [{'hypervisor_type': 'xen',
                                    'hypervisor_version': 1}]}])
        db.service_get_all_compute_by_host(self.context,
            instance['host']).AndReturn(
                    [{'compute_node': [{'hypervisor_type': 'xen',
                                        'hypervisor_version': 2}]}])
        self.mox.ReplayAll()
        self.assertRaises(exception.DestinationHypervisorTooOld,
                self.driver.schedule_live_migration, self.context,
                instance=instance, dest=dest,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)


class SchedulerDriverBaseTestCase(SchedulerTestCase):
    """Test cases for base scheduler driver class methods
       that can't will fail if the driver is changed"""

    def test_unimplemented_schedule_run_instance(self):
        fake_args = (1, 2, 3)
        fake_kwargs = {'cat': 'meow'}
        fake_request_spec = {'instance_properties':
                {'uuid': 'uuid'}}

        self.assertRaises(NotImplementedError,
                         self.driver.schedule_run_instance,
                         self.context, fake_request_spec, None, None, None,
                         None, None)

    def test_unimplemented_schedule_prep_resize(self):
        fake_args = (1, 2, 3)
        fake_kwargs = {'cat': 'meow'}
        fake_request_spec = {'instance_properties':
                {'uuid': 'uuid'}}

        self.assertRaises(NotImplementedError,
                         self.driver.schedule_prep_resize,
                         self.context, {},
                         fake_request_spec, {}, {}, {}, None)


class SchedulerDriverModuleTestCase(test.TestCase):
    """Test case for scheduler driver module methods"""

    def setUp(self):
        super(SchedulerDriverModuleTestCase, self).setUp()
        self.context = context.RequestContext('fake_user', 'fake_project')

    def test_cast_to_volume_host_update_db_with_volume_id(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'volume_id': 31337,
                       'extra_arg': 'meow'}
        queue = 'fake_queue'

        self.mox.StubOutWithMock(timeutils, 'utcnow')
        self.mox.StubOutWithMock(db, 'volume_update')
        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'cast')

        timeutils.utcnow().AndReturn('fake-now')
        db.volume_update(self.context, 31337,
                {'host': host, 'scheduled_at': 'fake-now'})
        rpc.queue_get_for(self.context, 'volume', host).AndReturn(queue)
        rpc.cast(self.context, queue,
                {'method': method,
                 'args': fake_kwargs})

        self.mox.ReplayAll()
        driver.cast_to_volume_host(self.context, host, method,
                **fake_kwargs)

    def test_cast_to_volume_host_update_db_without_volume_id(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'extra_arg': 'meow'}
        queue = 'fake_queue'

        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'cast')

        rpc.queue_get_for(self.context, 'volume', host).AndReturn(queue)
        rpc.cast(self.context, queue,
                {'method': method,
                 'args': fake_kwargs})

        self.mox.ReplayAll()
        driver.cast_to_volume_host(self.context, host, method,
                **fake_kwargs)

    def test_cast_to_compute_host_update_db_with_instance_uuid(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'instance_uuid': 'fake_uuid',
                       'extra_arg': 'meow'}
        queue = 'fake_queue'

        self.mox.StubOutWithMock(timeutils, 'utcnow')
        self.mox.StubOutWithMock(db, 'instance_update')
        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'cast')

        timeutils.utcnow().AndReturn('fake-now')
        db.instance_update(self.context, 'fake_uuid',
                {'host': host, 'scheduled_at': 'fake-now'})
        rpc.queue_get_for(self.context, 'compute', host).AndReturn(queue)
        rpc.cast(self.context, queue,
                {'method': method,
                 'args': fake_kwargs})

        self.mox.ReplayAll()
        driver.cast_to_compute_host(self.context, host, method,
                **fake_kwargs)

    def test_cast_to_compute_host_update_db_without_instance_uuid(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'extra_arg': 'meow'}
        queue = 'fake_queue'

        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'cast')

        rpc.queue_get_for(self.context, 'compute', host).AndReturn(queue)
        rpc.cast(self.context, queue,
                {'method': method,
                 'args': fake_kwargs})

        self.mox.ReplayAll()
        driver.cast_to_compute_host(self.context, host, method,
                **fake_kwargs)

    def test_cast_to_network_host(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'extra_arg': 'meow'}
        queue = 'fake_queue'

        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'cast')

        rpc.queue_get_for(self.context, 'network', host).AndReturn(queue)
        rpc.cast(self.context, queue,
                {'method': method,
                 'args': fake_kwargs})

        self.mox.ReplayAll()
        driver.cast_to_network_host(self.context, host, method,
                **fake_kwargs)

    def test_cast_to_host_compute_topic(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'extra_arg': 'meow'}

        self.mox.StubOutWithMock(driver, 'cast_to_compute_host')
        driver.cast_to_compute_host(self.context, host, method,
                **fake_kwargs)

        self.mox.ReplayAll()
        driver.cast_to_host(self.context, 'compute', host, method,
                **fake_kwargs)

    def test_cast_to_host_volume_topic(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'extra_arg': 'meow'}

        self.mox.StubOutWithMock(driver, 'cast_to_volume_host')
        driver.cast_to_volume_host(self.context, host, method,
                **fake_kwargs)

        self.mox.ReplayAll()
        driver.cast_to_host(self.context, 'volume', host, method,
                **fake_kwargs)

    def test_cast_to_host_network_topic(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'extra_arg': 'meow'}

        self.mox.StubOutWithMock(driver, 'cast_to_network_host')
        driver.cast_to_network_host(self.context, host, method,
                **fake_kwargs)

        self.mox.ReplayAll()
        driver.cast_to_host(self.context, 'network', host, method,
                **fake_kwargs)

    def test_cast_to_host_unknown_topic(self):
        host = 'fake_host1'
        method = 'fake_method'
        fake_kwargs = {'extra_arg': 'meow'}
        topic = 'unknown'
        queue = 'fake_queue'

        self.mox.StubOutWithMock(rpc, 'queue_get_for')
        self.mox.StubOutWithMock(rpc, 'cast')

        rpc.queue_get_for(self.context, topic, host).AndReturn(queue)
        rpc.cast(self.context, queue,
                {'method': method,
                 'args': fake_kwargs})

        self.mox.ReplayAll()
        driver.cast_to_host(self.context, topic, host, method,
                **fake_kwargs)

    def test_encode_instance(self):
        instance = {'id': 31337,
                    'test_arg': 'meow'}

        result = driver.encode_instance(instance, True)
        expected = {'id': instance['id'], '_is_precooked': False}
        self.assertDictMatch(result, expected)
        # Orig dict not changed
        self.assertNotEqual(result, instance)

        result = driver.encode_instance(instance, False)
        expected = {}
        expected.update(instance)
        expected['_is_precooked'] = True
        self.assertDictMatch(result, expected)
        # Orig dict not changed
        self.assertNotEqual(result, instance)
