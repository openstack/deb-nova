#    Copyright 2010 OpenStack Foundation
#    Copyright 2012 University Of Minho
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

import re
import uuid
from xml.dom import minidom

from lxml import etree
import mock
from mox3 import mox
from oslo_concurrency.fixture import lockutils as lock_fixture

from nova.compute import utils as compute_utils
from nova import exception
from nova.network import linux_net
from nova import objects
from nova import test
from nova.tests.unit import fake_network
from nova.tests.unit.virt.libvirt import fakelibvirt
from nova.virt.libvirt import firewall
from nova.virt import netutils
from nova.virt import virtapi

try:
    import libvirt
except ImportError:
    libvirt = fakelibvirt

_fake_network_info = fake_network.fake_get_instance_nw_info
_fake_stub_out_get_nw_info = fake_network.stub_out_nw_api_get_instance_nw_info
_ipv4_like = fake_network.ipv4_like


class NWFilterFakes:
    def __init__(self):
        self.filters = {}

    def nwfilterLookupByName(self, name):
        if name in self.filters:
            return self.filters[name]
        raise libvirt.libvirtError('Filter Not Found')

    def filterDefineXMLMock(self, xml):
        class FakeNWFilterInternal:
            def __init__(self, parent, name, u, xml):
                self.name = name
                self.uuid = u
                self.parent = parent
                self.xml = xml

            def XMLDesc(self, flags):
                return self.xml

            def undefine(self):
                del self.parent.filters[self.name]

        tree = etree.fromstring(xml)
        name = tree.get('name')
        u = tree.find('uuid')
        if u is None:
            u = uuid.uuid4().hex
        else:
            u = u.text
        if name not in self.filters:
            self.filters[name] = FakeNWFilterInternal(self, name, u, xml)
        else:
            if self.filters[name].uuid != u:
                raise libvirt.libvirtError(
                    "Mismatching name '%s' with uuid '%s' vs '%s'"
                    % (name, self.filters[name].uuid, u))
            self.filters[name].xml = xml
        return True


class FakeVirtAPI(virtapi.VirtAPI):
    def provider_fw_rule_get_all(self, context):
        return []


class IptablesFirewallTestCase(test.NoDBTestCase):
    def setUp(self):
        super(IptablesFirewallTestCase, self).setUp()
        self.useFixture(lock_fixture.ExternalLockFixture())

        class FakeLibvirtDriver(object):
            def nwfilterDefineXML(*args, **kwargs):
                """setup_basic_rules in nwfilter calls this."""
                pass

        self.fake_libvirt_connection = FakeLibvirtDriver()
        self.fw = firewall.IptablesFirewallDriver(
            FakeVirtAPI(),
            get_connection=lambda: self.fake_libvirt_connection)

    in_rules = [
      '# Generated by iptables-save v1.4.10 on Sat Feb 19 00:03:19 2011',
      '*nat',
      ':PREROUTING ACCEPT [1170:189210]',
      ':INPUT ACCEPT [844:71028]',
      ':OUTPUT ACCEPT [5149:405186]',
      ':POSTROUTING ACCEPT [5063:386098]',
      '# Completed on Tue Dec 18 15:50:25 2012',
      '# Generated by iptables-save v1.4.12 on Tue Dec 18 15:50:25 201;',
      '*mangle',
      ':PREROUTING ACCEPT [241:39722]',
      ':INPUT ACCEPT [230:39282]',
      ':FORWARD ACCEPT [0:0]',
      ':OUTPUT ACCEPT [266:26558]',
      ':POSTROUTING ACCEPT [267:26590]',
      '-A POSTROUTING -o virbr0 -p udp -m udp --dport 68 -j CHECKSUM '
      '--checksum-fill',
      'COMMIT',
      '# Completed on Tue Dec 18 15:50:25 2012',
      '# Generated by iptables-save v1.4.4 on Mon Dec  6 11:54:13 2010',
      '*filter',
      ':INPUT ACCEPT [969615:281627771]',
      ':FORWARD ACCEPT [0:0]',
      ':OUTPUT ACCEPT [915599:63811649]',
      ':nova-block-ipv4 - [0:0]',
      '[0:0] -A INPUT -i virbr0 -p tcp -m tcp --dport 67 -j ACCEPT ',
      '[0:0] -A FORWARD -d 192.168.122.0/24 -o virbr0 -m state --state RELATED'
      ',ESTABLISHED -j ACCEPT ',
      '[0:0] -A FORWARD -s 192.168.122.0/24 -i virbr0 -j ACCEPT ',
      '[0:0] -A FORWARD -i virbr0 -o virbr0 -j ACCEPT ',
      '[0:0] -A FORWARD -o virbr0 -j REJECT '
      '--reject-with icmp-port-unreachable ',
      '[0:0] -A FORWARD -i virbr0 -j REJECT '
      '--reject-with icmp-port-unreachable ',
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

    def _create_instance_ref(self,
                             uuid="74526555-9166-4893-a203-126bdcab0d67"):
        inst = objects.Instance(
            id=7,
            uuid=uuid,
            user_id="fake",
            project_id="fake",
            image_ref='155d900f-4e14-4e4c-a73d-069cbf4541e6',
            instance_type_id=1)
        inst.info_cache = objects.InstanceInfoCache()
        inst.info_cache.deleted = False
        return inst

    @mock.patch.object(objects.InstanceList, "get_by_security_group_id")
    @mock.patch.object(objects.SecurityGroupRuleList,
                       "get_by_security_group_id")
    @mock.patch.object(objects.SecurityGroupList, "get_by_instance")
    def test_static_filters(self, mock_secgroup, mock_secrule, mock_instlist):
        UUID = "2674993b-6adb-4733-abd9-a7c10cc1f146"
        SRC_UUID = "0e0a76b2-7c52-4bc0-9a60-d83017e42c1a"
        instance_ref = self._create_instance_ref(UUID)
        src_instance_ref = self._create_instance_ref(SRC_UUID)

        secgroup = objects.SecurityGroup(id=1,
                                         user_id='fake',
                                         project_id='fake',
                                         name='testgroup',
                                         description='test group')

        src_secgroup = objects.SecurityGroup(id=2,
                                             user_id='fake',
                                             project_id='fake',
                                             name='testsourcegroup',
                                             description='src group')

        r1 = objects.SecurityGroupRule(parent_group_id=secgroup['id'],
                                       protocol='icmp',
                                       from_port=-1,
                                       to_port=-1,
                                       cidr='192.168.11.0/24',
                                       grantee_group=None)

        r2 = objects.SecurityGroupRule(parent_group_id=secgroup['id'],
                                       protocol='icmp',
                                       from_port=8,
                                       to_port=-1,
                                       cidr='192.168.11.0/24',
                                       grantee_group=None)

        r3 = objects.SecurityGroupRule(parent_group_id=secgroup['id'],
                                       protocol='tcp',
                                       from_port=80,
                                       to_port=81,
                                       cidr='192.168.10.0/24',
                                       grantee_group=None)

        r4 = objects.SecurityGroupRule(parent_group_id=secgroup['id'],
                                       protocol='tcp',
                                       from_port=80,
                                       to_port=81,
                                       cidr=None,
                                       grantee_group=src_secgroup,
                                       group_id=src_secgroup['id'])

        r5 = objects.SecurityGroupRule(parent_group_id=secgroup['id'],
                                       protocol=None,
                                       cidr=None,
                                       grantee_group=src_secgroup,
                                       group_id=src_secgroup['id'])

        secgroup_list = objects.SecurityGroupList()
        secgroup_list.objects.append(secgroup)
        src_secgroup_list = objects.SecurityGroupList()
        src_secgroup_list.objects.append(src_secgroup)
        instance_ref.security_groups = secgroup_list
        src_instance_ref.security_groups = src_secgroup_list

        def _fake_secgroup(ctxt, instance):
            if instance.uuid == UUID:
                return instance_ref.security_groups
            else:
                return src_instance_ref.security_groups

        mock_secgroup.side_effect = _fake_secgroup

        def _fake_secrule(ctxt, id):
            if id == secgroup.id:
                rules = objects.SecurityGroupRuleList()
                rules.objects.extend([r1, r2, r3, r4, r5])
                return rules
            else:
                return []

        mock_secrule.side_effect = _fake_secrule

        def _fake_instlist(ctxt, id):
            if id == src_secgroup['id']:
                insts = objects.InstanceList()
                insts.objects.append(src_instance_ref)
                return insts
            else:
                insts = objects.InstanceList()
                insts.objects.append(instance_ref)
                return insts

        mock_instlist.side_effect = _fake_instlist

        def fake_iptables_execute(*cmd, **kwargs):
            process_input = kwargs.get('process_input', None)
            if cmd == ('ip6tables-save', '-c'):
                return '\n'.join(self.in6_filter_rules), None
            if cmd == ('iptables-save', '-c'):
                return '\n'.join(self.in_rules), None
            if cmd == ('iptables-restore', '-c'):
                lines = process_input.split('\n')
                if '*filter' in lines:
                    self.out_rules = lines
                return '', ''
            if cmd == ('ip6tables-restore', '-c',):
                lines = process_input.split('\n')
                if '*filter' in lines:
                    self.out6_rules = lines
                return '', ''

        network_model = _fake_network_info(self.stubs, 1)

        linux_net.iptables_manager.execute = fake_iptables_execute

        self.stubs.Set(compute_utils, 'get_nw_info_for_instance',
                       lambda instance: network_model)

        self.fw.prepare_instance_filter(instance_ref, network_model)
        self.fw.apply_instance_filter(instance_ref, network_model)

        in_rules = filter(lambda l: not l.startswith('#'),
                          self.in_rules)
        for rule in in_rules:
            if 'nova' not in rule:
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

        regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p icmp '
                           '-s 192.168.11.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "ICMP acceptance rule wasn't added")

        regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p icmp -m icmp '
                           '--icmp-type 8 -s 192.168.11.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "ICMP Echo Request acceptance rule wasn't added")

        for ip in network_model.fixed_ips():
            if ip['version'] != 4:
                continue
            regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p tcp -m multiport '
                               '--dports 80:81 -s %s' % ip['address'])
            self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                            "TCP port 80/81 acceptance rule wasn't added")
            regex = re.compile('\[0\:0\] -A .* -j ACCEPT -s '
                               '%s' % ip['address'])
            self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                            "Protocol/port-less acceptance rule wasn't added")

        regex = re.compile('\[0\:0\] -A .* -j ACCEPT -p tcp '
                           '-m multiport --dports 80:81 -s 192.168.10.0/24')
        self.assertTrue(len(filter(regex.match, self.out_rules)) > 0,
                        "TCP port 80/81 acceptance rule wasn't added")

    def test_filters_for_instance_with_ip_v6(self):
        self.flags(use_ipv6=True)
        network_info = _fake_network_info(self.stubs, 1)
        rulesv4, rulesv6 = self.fw._filters_for_instance("fake", network_info)
        self.assertEqual(len(rulesv4), 2)
        self.assertEqual(len(rulesv6), 1)

    def test_filters_for_instance_without_ip_v6(self):
        self.flags(use_ipv6=False)
        network_info = _fake_network_info(self.stubs, 1)
        rulesv4, rulesv6 = self.fw._filters_for_instance("fake", network_info)
        self.assertEqual(len(rulesv4), 2)
        self.assertEqual(len(rulesv6), 0)

    @mock.patch.object(objects.SecurityGroupList, "get_by_instance")
    def test_multinic_iptables(self, mock_secgroup):
        mock_secgroup.return_value = objects.SecurityGroupList()

        ipv4_rules_per_addr = 1
        ipv4_addr_per_network = 2
        ipv6_rules_per_addr = 1
        ipv6_addr_per_network = 1
        networks_count = 5
        instance_ref = self._create_instance_ref()
        network_info = _fake_network_info(self.stubs, networks_count,
                                ipv4_addr_per_network)
        network_info[0]['network']['subnets'][0]['meta']['dhcp_server'] = \
            '1.1.1.1'
        ipv4_len = len(self.fw.iptables.ipv4['filter'].rules)
        ipv6_len = len(self.fw.iptables.ipv6['filter'].rules)
        inst_ipv4, inst_ipv6 = self.fw.instance_rules(instance_ref,
                                                      network_info)
        self.fw.prepare_instance_filter(instance_ref, network_info)
        ipv4 = self.fw.iptables.ipv4['filter'].rules
        ipv6 = self.fw.iptables.ipv6['filter'].rules
        ipv4_network_rules = len(ipv4) - len(inst_ipv4) - ipv4_len
        ipv6_network_rules = len(ipv6) - len(inst_ipv6) - ipv6_len
        # Extra rules are for the DHCP request
        rules = (ipv4_rules_per_addr * ipv4_addr_per_network *
                 networks_count) + 2
        self.assertEqual(ipv4_network_rules, rules)
        self.assertEqual(ipv6_network_rules,
                  ipv6_rules_per_addr * ipv6_addr_per_network * networks_count)

    def test_do_refresh_security_group_rules(self):
        instance_ref = self._create_instance_ref()
        self.mox.StubOutWithMock(self.fw,
                                 'instance_rules')
        self.mox.StubOutWithMock(self.fw,
                                 'add_filters_for_instance',
                                 use_mock_anything=True)
        self.mox.StubOutWithMock(self.fw.iptables.ipv4['filter'],
                                 'has_chain')

        self.fw.instance_rules(instance_ref,
                               mox.IgnoreArg()).AndReturn((None, None))
        self.fw.add_filters_for_instance(instance_ref, mox.IgnoreArg(),
                                         mox.IgnoreArg(), mox.IgnoreArg())
        self.fw.instance_rules(instance_ref,
                               mox.IgnoreArg()).AndReturn((None, None))
        self.fw.iptables.ipv4['filter'].has_chain(mox.IgnoreArg()
                                                  ).AndReturn(True)
        self.fw.add_filters_for_instance(instance_ref, mox.IgnoreArg(),
                                         mox.IgnoreArg(), mox.IgnoreArg())
        self.mox.ReplayAll()

        self.fw.prepare_instance_filter(instance_ref, mox.IgnoreArg())
        self.fw.instance_info[instance_ref['id']] = (instance_ref, None)
        self.fw.do_refresh_security_group_rules("fake")

    def test_do_refresh_security_group_rules_instance_gone(self):
        instance1 = {'id': 1, 'uuid': 'fake-uuid1'}
        instance2 = {'id': 2, 'uuid': 'fake-uuid2'}
        self.fw.instance_info = {1: (instance1, 'netinfo1'),
                                 2: (instance2, 'netinfo2')}
        mock_filter = mock.MagicMock()
        with mock.patch.dict(self.fw.iptables.ipv4, {'filter': mock_filter}):
            mock_filter.has_chain.return_value = False
            with mock.patch.object(self.fw, 'instance_rules') as mock_ir:
                mock_ir.return_value = (None, None)
                self.fw.do_refresh_security_group_rules('secgroup')
                self.assertEqual(2, mock_ir.call_count)
            # NOTE(danms): Make sure that it is checking has_chain each time,
            # continuing to process all the instances, and never adding the
            # new chains back if has_chain() is False
            mock_filter.has_chain.assert_has_calls([mock.call('inst-1'),
                                                    mock.call('inst-2')],
                                                   any_order=True)
            self.assertEqual(0, mock_filter.add_chain.call_count)

    @mock.patch.object(objects.InstanceList, "get_by_security_group_id")
    @mock.patch.object(objects.SecurityGroupRuleList,
                       "get_by_security_group_id")
    @mock.patch.object(objects.SecurityGroupList, "get_by_instance")
    def test_unfilter_instance_undefines_nwfilter(self,
                                                  mock_secgroup,
                                                  mock_secrule,
                                                  mock_instlist):
        fakefilter = NWFilterFakes()
        _xml_mock = fakefilter.filterDefineXMLMock
        self.fw.nwfilter._conn.nwfilterDefineXML = _xml_mock
        _lookup_name = fakefilter.nwfilterLookupByName
        self.fw.nwfilter._conn.nwfilterLookupByName = _lookup_name
        instance_ref = self._create_instance_ref()

        mock_secgroup.return_value = objects.SecurityGroupList()

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        self.fw.prepare_instance_filter(instance_ref, network_info)
        self.fw.apply_instance_filter(instance_ref, network_info)
        original_filter_count = len(fakefilter.filters)
        self.fw.unfilter_instance(instance_ref, network_info)

        # should undefine just the instance filter
        self.assertEqual(original_filter_count - len(fakefilter.filters), 1)

    @mock.patch.object(FakeVirtAPI, "provider_fw_rule_get_all")
    @mock.patch.object(objects.SecurityGroupList, "get_by_instance")
    def test_provider_firewall_rules(self, mock_secgroup, mock_fwrules):
        mock_secgroup.return_value = objects.SecurityGroupList()

        # setup basic instance data
        instance_ref = self._create_instance_ref()
        # FRAGILE: peeks at how the firewall names chains
        chain_name = 'inst-%s' % instance_ref['id']

        # create a firewall via setup_basic_filtering like libvirt_conn.spawn
        # should have a chain with 0 rules
        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        self.assertIn('provider', self.fw.iptables.ipv4['filter'].chains)
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(0, len(rules))

        # add a rule angd send the update message, check for 1 rule
        mock_fwrules.return_value = [{'protocol': 'tcp',
                                      'cidr': '10.99.99.99/32',
                                      'from_port': 1,
                                      'to_port': 65535}]
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(1, len(rules))

        # Add another, refresh, and make sure number of rules goes to two
        mock_fwrules.return_value = [{'protocol': 'tcp',
                                      'cidr': '10.99.99.99/32',
                                      'from_port': 1,
                                      'to_port': 65535},
                                     {'protocol': 'udp',
                                      'cidr': '10.99.99.99/32',
                                      'from_port': 1,
                                      'to_port': 65535}]
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
        mock_fwrules.return_value = [{'protocol': 'udp',
                                      'cidr': '10.99.99.99/32',
                                      'from_port': 1,
                                      'to_port': 65535}]
        self.fw.refresh_provider_fw_rules()
        rules = [rule for rule in self.fw.iptables.ipv4['filter'].rules
                      if rule.chain == 'provider']
        self.assertEqual(1, len(rules))


class NWFilterTestCase(test.NoDBTestCase):
    def setUp(self):
        super(NWFilterTestCase, self).setUp()

        class Mock(object):
            pass

        self.fake_libvirt_connection = Mock()

        self.fw = firewall.NWFilterFirewall(
            FakeVirtAPI(),
            lambda: self.fake_libvirt_connection)

    def _create_security_group(self, instance_ref):
        secgroup = objects.SecurityGroup(id=1,
                                         user_id='fake',
                                         project_id='fake',
                                         name='testgroup',
                                         description='test group description')

        secgroup_list = objects.SecurityGroupList()
        secgroup_list.objects.append(secgroup)
        instance_ref.security_groups = secgroup_list

        return secgroup

    def _create_instance(self):
        inst = objects.Instance(
            id=7,
            uuid="74526555-9166-4893-a203-126bdcab0d67",
            user_id="fake",
            project_id="fake",
            image_ref='155d900f-4e14-4e4c-a73d-069cbf4541e6',
            instance_type_id=1)
        inst.info_cache = objects.InstanceInfoCache()
        inst.info_cache.deleted = False
        return inst

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
        self._create_security_group(instance_ref)

        def _ensure_all_called(mac, allow_dhcp):
            instance_filter = 'nova-instance-%s-%s' % (instance_ref['name'],
                    mac.translate({ord(':'): None}))
            requiredlist = ['no-arp-spoofing', 'no-ip-spoofing',
                             'no-mac-spoofing']
            required_not_list = []
            if allow_dhcp:
                requiredlist.append('allow-dhcp-server')
            else:
                required_not_list.append('allow-dhcp-server')
            for required in requiredlist:
                self.assertTrue(required in
                                self.recursive_depends[instance_filter],
                                "Instance's filter does not include %s" %
                                required)
            for required_not in required_not_list:
                self.assertFalse(required_not in
                    self.recursive_depends[instance_filter],
                    "Instance filter includes %s" % required_not)

        network_info = _fake_network_info(self.stubs, 1)
        # since there is one (network_info) there is one vif
        # pass this vif's mac to _ensure_all_called()
        # to set the instance_filter properly
        mac = network_info[0]['address']
        network_info[0]['network']['subnets'][0]['meta']['dhcp_server'] = \
            '1.1.1.1'
        self.fw.setup_basic_filtering(instance_ref, network_info)
        allow_dhcp = True
        _ensure_all_called(mac, allow_dhcp)

        network_info[0]['network']['subnets'][0]['meta']['dhcp_server'] = None
        self.fw.setup_basic_filtering(instance_ref, network_info)
        allow_dhcp = False
        _ensure_all_called(mac, allow_dhcp)

    def test_unfilter_instance_undefines_nwfilters(self):
        fakefilter = NWFilterFakes()
        self.fw._conn.nwfilterDefineXML = fakefilter.filterDefineXMLMock
        self.fw._conn.nwfilterLookupByName = fakefilter.nwfilterLookupByName

        instance_ref = self._create_instance()
        self._create_security_group(instance_ref)

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        original_filter_count = len(fakefilter.filters)
        self.fw.unfilter_instance(instance_ref, network_info)
        self.assertEqual(original_filter_count - len(fakefilter.filters), 1)

    def test_redefining_nwfilters(self):
        fakefilter = NWFilterFakes()
        self.fw._conn.nwfilterDefineXML = fakefilter.filterDefineXMLMock
        self.fw._conn.nwfilterLookupByName = fakefilter.nwfilterLookupByName

        instance_ref = self._create_instance()
        self._create_security_group(instance_ref)

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)
        self.fw.setup_basic_filtering(instance_ref, network_info)

    def test_nwfilter_parameters(self):
        fakefilter = NWFilterFakes()
        self.fw._conn.nwfilterDefineXML = fakefilter.filterDefineXMLMock
        self.fw._conn.nwfilterLookupByName = fakefilter.nwfilterLookupByName

        instance_ref = self._create_instance()
        self._create_security_group(instance_ref)

        network_info = _fake_network_info(self.stubs, 1)
        self.fw.setup_basic_filtering(instance_ref, network_info)

        vif = network_info[0]
        nic_id = vif['address'].replace(':', '')
        instance_filter_name = self.fw._instance_filter_name(instance_ref,
                                                             nic_id)
        f = fakefilter.nwfilterLookupByName(instance_filter_name)
        tree = etree.fromstring(f.xml)

        for fref in tree.findall('filterref'):
            parameters = fref.findall('./parameter')
            for parameter in parameters:
                subnet_v4, subnet_v6 = vif['network']['subnets']
                if parameter.get('name') == 'IP':
                    self.assertTrue(_ipv4_like(parameter.get('value'),
                                                             '192.168'))
                elif parameter.get('name') == 'DHCPSERVER':
                    dhcp_server = subnet_v4.get('dhcp_server')
                    self.assertEqual(parameter.get('value'), dhcp_server)
                elif parameter.get('name') == 'RASERVER':
                    ra_server = subnet_v6['gateway']['address'] + "/128"
                    self.assertEqual(parameter.get('value'), ra_server)
                elif parameter.get('name') == 'PROJNET':
                    ipv4_cidr = subnet_v4['cidr']
                    net, mask = netutils.get_net_and_mask(ipv4_cidr)
                    self.assertEqual(parameter.get('value'), net)
                elif parameter.get('name') == 'PROJMASK':
                    ipv4_cidr = subnet_v4['cidr']
                    net, mask = netutils.get_net_and_mask(ipv4_cidr)
                    self.assertEqual(parameter.get('value'), mask)
                elif parameter.get('name') == 'PROJNET6':
                    ipv6_cidr = subnet_v6['cidr']
                    net, prefix = netutils.get_net_and_prefixlen(ipv6_cidr)
                    self.assertEqual(parameter.get('value'), net)
                elif parameter.get('name') == 'PROJMASK6':
                    ipv6_cidr = subnet_v6['cidr']
                    net, prefix = netutils.get_net_and_prefixlen(ipv6_cidr)
                    self.assertEqual(parameter.get('value'), prefix)
                else:
                    raise exception.InvalidParameterValue('unknown parameter '
                                                          'in filter')

    def test_multinic_base_filter_selection(self):
        fakefilter = NWFilterFakes()
        self.fw._conn.nwfilterDefineXML = fakefilter.filterDefineXMLMock
        self.fw._conn.nwfilterLookupByName = fakefilter.nwfilterLookupByName

        instance_ref = self._create_instance()
        self._create_security_group(instance_ref)

        network_info = _fake_network_info(self.stubs, 2)
        network_info[0]['network']['subnets'][0]['meta']['dhcp_server'] = \
            '1.1.1.1'

        self.fw.setup_basic_filtering(instance_ref, network_info)

        def assert_filterref(instance, vif, expected=None):
            expected = expected or []
            nic_id = vif['address'].replace(':', '')
            filter_name = self.fw._instance_filter_name(instance, nic_id)
            f = fakefilter.nwfilterLookupByName(filter_name)
            tree = etree.fromstring(f.xml)
            frefs = [fr.get('filter') for fr in tree.findall('filterref')]
            self.assertEqual(set(expected), set(frefs))

        assert_filterref(instance_ref, network_info[0],
                         expected=['nova-base'])
        assert_filterref(instance_ref, network_info[1],
                         expected=['nova-nodhcp'])
