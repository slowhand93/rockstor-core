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
from tempfile import mkstemp
from shutil import move
from django.db import transaction
from django.conf import settings
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from storageadmin.models import (NetworkConnection, NetworkDevice, Appliance,
                                 EthernetConnection, TeamConnection)
from storageadmin.util import handle_exception
from storageadmin.serializers import (NetworkDeviceSerializer, NetworkConnectionSerializer)
from system.osi import (config_network_device, get_net_config, update_issue)
from system.services import superctl
from system import network
import rest_framework_custom as rfc

import logging
logger = logging.getLogger(__name__)


class NetworkMixin(object):

    @staticmethod
    def _update_ni_obj(nio, values):
        nio.dname = values.get('dname', nio.dname)
        nio.mac = values.get('mac', nio.mac)
        nio.method = values.get('method', 'manual')
        nio.autoconnect = values.get('autoconnect', 'no')
        nio.netmask = values.get('netmask', nio.netmask)
        nio.ipaddr = values.get('ipaddr', nio.ipaddr)
        nio.gateway = values.get('gateway', nio.gateway)
        nio.dns_servers = values.get('dns_servers', nio.dns_servers)
        nio.ctype = values.get('ctype', nio.ctype)
        nio.dtype = values.get('dtype', nio.dtype)
        nio.dspeed = values.get('dspeed', nio.dspeed)
        nio.state = values.get('state', nio.state)
        return nio

    @staticmethod
    def _update_nginx(ipaddr=None):
        #update nginx config and restart the service
        conf = '%s/etc/nginx/nginx.conf' % settings.ROOT_DIR
        fo, npath = mkstemp()
        with open(conf) as ifo, open(npath, 'w') as tfo:
            for line in ifo.readlines():
                if (re.search('listen.*80 default_server', line) is not None):
                    substr = 'listen 80'
                    if (ipaddr is not None):
                        substr = 'listen %s:80' % ipaddr
                    line = re.sub(r'listen.*80', substr, line)
                elif (re.search('listen.*443 default_server', line) is not None):
                    substr = 'listen 443'
                    if (ipaddr is not None):
                        substr = 'listen %s:443' % ipaddr
                    line = re.sub(r'listen.*443', substr, line)
                tfo.write(line)
        move(npath, conf)
        superctl('nginx', 'restart')

    @staticmethod
    @transaction.atomic
    def _update_or_create_ctype(co, ctype, config):
        if (ctype == '802-3-ethernet'):
            try:
                eco = EthernetConnection.objects.get(connection=co)
                eco.mac = config['mac']
                eco.cloned_mac = config['cloned_mac']
                eco.mtu = config['mtu']
                eco.save()
            except EthernetConnection.DoesNotExist:
                EthernetConnection.objects.create(connection=co, **config)
        elif (ctype == 'team'):
            try:
                tco = TeamConnection.objects.get(connection=co)
                tco.name = config['name']
                tco.config = config['config']
                tco.save()
            except TeamConnection.DoesNotExist:
                TeamConnection.objects.create(connection=co, **config)

        #elif's for other types of connections

    @staticmethod
    @transaction.atomic
    def _update_master(co, config, defer_list=None):
        if ('master' not in config):
            return config
        try:
            co.master = NetworkConnection.objects.get(name=config['master'])
        except NetworkConnection.DoesNotExist:
            if (not isinstance(defer_list, list)):
                raise
            defer_list.append({'uuid': co.uuid, 'master': config['master']})
        del(config['master'])

    @classmethod
    @transaction.atomic
    def _refresh_connections(cls):
        cmap = network.connections()
        defer_master_updates = []
        for nco in NetworkConnection.objects.all():
            if (nco.uuid not in cmap):
                nco.delete()
                continue
            config = cmap[nco.uuid]
            if ('ctype' in config):
                ctype = config['ctype']
                cls._update_or_create_ctype(nco, ctype, config[ctype])
                del(config[ctype])
                del(config['ctype'])
            cls._update_master(nco, config, defer_master_updates)
            NetworkConnection.objects.filter(uuid=nco.uuid).update(**config)
            del cmap[nco.uuid]
        for uuid in cmap:
            #new connections not yet in administrative state.
            config = cmap[uuid]
            config['uuid'] = uuid
            ctype = ctype_d = None
            if ('ctype' in config):
                ctype = config['ctype']
                ctype_d = config[ctype]
                del(config[ctype])
                del(config['ctype'])
            if ('master' in config):
                defer_master_updates.append({'uuid': uuid, 'master': config['master']})
                del(config['master'])
            nco = NetworkConnection.objects.create(**config)
            if (ctype is not None):
                cls._update_or_create_ctype(nco, ctype, ctype_d)
        for e in defer_master_updates:
            slave_co = NetworkConnection.objects.get(uuid=e['uuid'])
            slave_co.master = NetworkConnection.objects.get(name=e['master'])
            slave_co.save()

    @staticmethod
    @transaction.atomic
    def _refresh_devices():
        dmap = network.devices()
        def update_connection(dconfig):
            if ('connection' in dconfig):
                try:
                    dconfig['connection'] = NetworkConnection.objects.get(name=dconfig['connection'])
                except NetworkConnection.DoesNotExist:
                    dconfig['connection'] = None

        for ndo in NetworkDevice.objects.all():
            if (ndo.name not in dmap):
                ndo.delete()
                continue
            dconfig = dmap[ndo.name]
            update_connection(dconfig)
            NetworkDevice.objects.filter(name=ndo.name).update(**dconfig)
            del dmap[ndo.name]
        for dev in dmap:
            dconfig = dmap[dev]
            dconfig['name'] = dev
            update_connection(dconfig)
            NetworkDevice.objects.create(**dconfig)


class NetworkDeviceListView(rfc.GenericView, NetworkMixin):
    serializer_class = NetworkDeviceSerializer

    def get_queryset(self, *args, **kwargs):
        with self._handle_exception(self.request):
            self._refresh_devices()
            #don't return unmanaged devices
            #return NetworkDevice.objects.filter(~Q(state='10 (unmanaged)'))
            return NetworkDevice.objects.all()

class NetworkConnectionListView(rfc.GenericView, NetworkMixin):
    serializer_class = NetworkConnectionSerializer
    ctypes = ('ethernet', 'team', 'bond')
    team_profiles = ('broadcast', 'roundrobin', 'activebackup', 'loadbalance', 'lacp')
    #ethtool is the default link watcher.
    runners = {
        'broadcast': {'name': 'broadcast'},
        'roundrobin': {'name': 'roundrobin'},
        'activebackup': {'name': 'activebackup'},
        'loadbalance': {'name': 'loadbalance'},
        'lacp': {'name': 'lacp'},
    }
    config_methods = ('auto', 'manual')

    def get_queryset(self, *args, **kwargs):
        with self._handle_exception(self.request):
            self._refresh_connections()
            return NetworkConnection.objects.all()

    @transaction.atomic
    def post(self, request):
        with self._handle_exception(request):
            logger.debug('request data = %s' % request.data)
            name = request.data.get('name')
            if (NetworkConnection.objects.filter(name=name).exists()):
                e_msg = ('Connection name(%s) is already in use. Choose a different name.' % con_name)
                handle_exception(Exception(e_msg), request)

            #connection type can be one of ethernet, team or bond
            ctype = request.data.get('ctype')
            if (ctype not in self.ctypes):
                e_msg = ('Unsupported connection type(%s). Supported ones include: %s' % (con_type, self.con_types))
                handle_exception(Exception(e_msg), request)
            if (ctype == 'team'):
                #gather required input for team
                team_profile = request.data.get('team_profile')
                if (team_profile not in self.team_profiles):
                    e_msg = ('Unsupported team profile(%s). Supported ones include: %s' % (team_profile, self.team_profiles))
                    handle_exception(Exception(e_msg), request)

                #comma separated list of devices to add to the team as slave connections.
                devices = request.data.get('devices')
                for d in devies:
                    try:
                        ndo = NetworkDevice.objects.get(name=d)
                        #if device belongs to another connection, change it.
                    except NetworkDevice.DoesNotExist:
                        e_msg = ('Unknown network device(%s)' % d)
                        handle_exception(Exception(e_msg), request)

            elif (ctype == 'ethernet'):
                device = request.data.get('device')
                try:
                    ndo = NetworkDevice.objects.get(name=device)
                except NetworkDevice.DoesNotExist:
                    e_msg = ('Network device(%s) does not exist' % device)
                    handle_exception(Exception(e_msg), request)

            elif (con_type == 'bond'):
                #gather required input for bond
                pass

            #auto of manual
            method = request.data.get('method')
            if (method not in self.config_methods):
                e_msg = ('Unsupported config method(%s). Supported ones include: %s' % (method, self.config_methods))
                handle_exception(Exception(e_msg), request)
            if (method == 'manual'):
                #ipaddr is of the format <IP>/<netmask>. eg: 192.168.1.2/24. If netmask is not given, it defaults to 32.
                ipaddr = request.data.get('ipaddr')
                gateway = request.data.get('gateway')
                dns_servers = request.data.get('dns_servers', None)
                search_domains = request.data.get('search_domains', None)

            return Response()


class NetworkConnectionDetailView(rfc.GenericView, NetworkMixin):
    serializer_class = NetworkConnectionSerializer

    def get(self, *args, **kwargs):
        try:
            data = NetworkConnection.objects.get(id=self.kwargs['id'])
            sdata = NetworkConnectionSerializer(data)
            return Response(sdata.data)
        except NetworkConnection.DoesNotExist:
            raise NotFound(detail=None)

    @staticmethod
    def _nco(request, id):
        try:
            return NetworkConnection.objects.get(id=id)
        except NetworkConnection.DoesNotExist:
            e_msg = ('Network connection(%s) does not exist.' % id)
            handle_exception(Exception(e_msg), request)

    def put(self, request, id):
        with self._handle_exception(request):
            nco = self._nco(request, id)
            return Response(NetworkConnectionSerializer(nco).data)

    @transaction.atomic
    def delete(self, request, id):
        with self._handle_exception(request):
            nco = self._nco(request, id)
            #delete any related connections using nmcli
            for snco in nco.networkconnection_set.all():
                network.delete_connection(snco.uuid)
            network.delete_connection(nco.uuid)
            nco.delete()
            return Response()

    @transaction.atomic
    def post(self, request, id, switch):
        #switch the connection up, down or reload.
        with self._handle_exception(request):
            nco = self._nco(request, id)
            network.toggle_connection(nco.uuid, switch)
            return Response(NetworkConnectionSerializer(nco).data)


class NetworkStateView(rfc.GenericView, NetworkMixin):
    serializer_class = NetworkConnectionSerializer

    def get_queryset(self, *args, **kwargs):
        return NetworkConnection.objects.all()

    def post(self, request):
        with self._handle_exception(request):
            self._refresh_connections()
            self._refresh_devices()
            ns = NetworkConnectionSerializer(NetworkConnection.objects.all(), many=True)
            return Response(ns.data)
