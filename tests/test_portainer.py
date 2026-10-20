import json
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'agent'))
from aquahub_agent.core import Application,Config
from aquahub_agent.security import ApiError,AuthPolicy,RequestContext,process_start_time
from aquahub_agent import portainer
from test_core import FakeDocker


class PortainerDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        self.container={'id':'a'*64,'name':'/aqua-portainer','image':'portainer/portainer-ce:2.45.2','running':True,
                        'ports':{'9443/tcp':[{'HostIp':'127.0.0.1','HostPort':'9443'}]}}
        self.commands=[]
        self.token='b'*64
    def run(self,args,**kwargs):
        self.commands.append(args)
        if args[0]=='ps':return 'a'*64
        if args[0]=='inspect':return json.dumps(self.container)
        if args[0]=='logs':return 'setup_token='+self.token if self.token else ''
        if args[0]=='restart':self.token='c'*64;return 'a'*64
        raise AssertionError('Unexpected Docker command')


class PortainerSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();base=Path(self.temp.name)
        self.docker=PortainerDocker()
        self.app=Application(Config(base/'rpc.sock',base/'state',no_poll=True),docker=self.docker,policy=AuthPolicy(os.getuid(),privileged=False))
        self.service=portainer.register(self.app)
        self.context=RequestContext(os.getuid(),os.getpid(),process_start_time(os.getpid()))
        self.initialized=False;self.received=[]
        self.service._http=self.http
    def tearDown(self):self.app.close();self.temp.cleanup()
    def http(self,path,body=None,setup_token=None):
        if path=='/api/users/admin/check':return 204 if self.initialized else 404
        if path=='/api/users/admin/init':
            self.received.append((body,setup_token));self.initialized=True;return 200
        raise AssertionError(path)
    def call(self,method,params=None):return self.app.dispatch(method,params or {},self.context)
    def finish(self,job):
        for _ in range(300):
            result=self.app.store.job(job['id'])
            if result['status'] not in ('running','queued'):return result
            time.sleep(.01)
        self.fail('Portainer setup job did not complete')
    def credentials(self):return {'username':'qa_admin','password':'DistinctSecret-Aa123456!'}

    def test_uninitialized_status_has_no_token_and_requires_native_setup(self):
        status=self.call('portainer.status')
        self.assertTrue(status['managed']);self.assertTrue(status['setup_required'])
        self.assertNotIn(self.docker.token,json.dumps(status))

    def test_existing_administrator_is_never_replaced(self):
        self.initialized=True
        with self.assertRaises(ApiError) as caught:self.call('portainer.initialize',self.credentials())
        self.assertEqual(caught.exception.code,'portainer_already_initialized')
        self.assertFalse(self.received);self.assertFalse(self.app.store.jobs())

    def test_success_creates_account_without_persisting_password_or_setup_token(self):
        credentials=self.credentials()
        job=self.call('portainer.initialize',credentials)
        result=self.finish(job)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(self.received,[( {'Username':credentials['username'],'Password':credentials['password']},'b'*64)])
        audit=json.dumps({'jobs':self.app.store.jobs(),'events':self.app.store.events()})
        self.assertNotIn(credentials['password'],audit);self.assertNotIn('b'*64,audit)
        self.assertTrue(self.call('portainer.status')['initialized'])

    def test_expired_setup_restarts_only_verified_portainer_then_retries_once(self):
        calls=[]
        def expired_once(path,body=None,setup_token=None):
            if path=='/api/users/admin/check':return 204 if self.initialized else 404
            calls.append(setup_token)
            if len(calls)==1:return 503
            self.initialized=True;return 200
        self.service._http=expired_once
        result=self.finish(self.call('portainer.initialize',self.credentials()))
        self.assertEqual(result['status'],'succeeded');self.assertEqual(calls,['b'*64,'c'*64])
        self.assertEqual([cmd for cmd in self.docker.commands if cmd[0]=='restart'],[['restart','--time','10','a'*64]])

    def test_missing_token_is_recovered_by_safe_restart(self):
        self.docker.token=None
        self.assertEqual(self.finish(self.call('portainer.initialize',self.credentials()))['status'],'succeeded')
        self.assertEqual(self.received[0][1],'c'*64)

    def test_unsafe_name_image_binding_or_identity_never_receives_credentials(self):
        changes=[{'name':'/foreign'},{'image':'alpine:latest'},{'id':'d'*64},{'running':False},
                 {'ports':{'9443/tcp':[{'HostIp':'0.0.0.0','HostPort':'9443'}]}},
                 {'ports':{'9443/tcp':[{'HostIp':'127.0.0.1','HostPort':'19443'}]}}]
        original=self.docker.container.copy()
        for change in changes:
            with self.subTest(change=change):
                self.docker.container={**original,**change}
                with self.assertRaises(ApiError):self.call('portainer.initialize',self.credentials())
        self.assertFalse(self.received);self.assertFalse(any(cmd[0]=='restart' for cmd in self.docker.commands))

    def test_input_validation_precedes_all_docker_work(self):
        values=[{'username':'bad\nname','password':'StrongPassword123!'}, {'username':'admin','password':'short'},
                {'username':'admin','password':'é'*40}, {'username':'admin','password':'StrongPassword123!','token':'inject'}]
        for value in values:
            with self.assertRaises(ApiError):self.call('portainer.initialize',value)
        self.assertFalse(self.docker.commands)

    def test_foreign_named_portainer_is_read_only_and_preserved(self):
        with patch.object(self.docker,'run',return_value=''),patch.object(self.docker,'portainer',return_value={'available':True,'name':'portainer','url':'https://127.0.0.1:9443'}):
            result=self.call('portainer.status')
        self.assertFalse(result['managed']);self.assertFalse(result['setup_required'])

    def test_racing_administrator_creation_during_job_is_not_overwritten(self):
        checks=iter([False,True])
        with patch.object(self.service,'_initialized',side_effect=lambda:next(checks)):
            result=self.finish(self.call('portainer.initialize',self.credentials()))
        self.assertEqual(result['status'],'failed');self.assertEqual(result['error']['code'],'portainer_already_initialized')
        self.assertFalse(self.received)

    def test_real_api_timeout_303_is_recognized_without_following_redirects(self):
        from unittest.mock import Mock
        expired=urllib.error.HTTPError(portainer.URL+'/api/users/admin/check',303,'See Other',{},io.BytesIO(b'{"message":"Administrator initialization timeout"}'))
        opener=Mock();opener.open.side_effect=expired
        with patch('aquahub_agent.portainer.urllib.request.build_opener',return_value=opener):
            self.assertEqual(portainer.Portainer._http(self.service,'/api/users/admin/check'),303)
        unknown=urllib.error.HTTPError(portainer.URL+'/api/users/admin/check',303,'See Other',{},io.BytesIO(b'{"message":"redirect elsewhere"}'))
        opener.open.side_effect=unknown
        with patch('aquahub_agent.portainer.urllib.request.build_opener',return_value=opener):
            with self.assertRaises(ApiError):portainer.Portainer._http(self.service,'/api/users/admin/check')
        self.assertFalse(any(command[0]=='restart' for command in self.docker.commands))


if __name__=='__main__':unittest.main()
