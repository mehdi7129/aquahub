from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"agent"))
from aquahub_agent.docker import Docker, SERVICES, PROJECT
from aquahub_agent.security import ApiError


class DockerAdapterTests(unittest.TestCase):
    def test_remote_context_is_refused(self):
        with self.assertRaises(ValueError):
            Docker("remote-server")

    def test_clean_environment_no_shell_and_combined_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory)/"fake-docker"
            script.write_text("#!/usr/bin/python3\nimport json,os,sys\nprint(json.dumps({'argv':sys.argv[1:],'host':os.environ.get('DOCKER_HOST'),'context':os.environ.get('DOCKER_CONTEXT'),'config':os.environ.get('DOCKER_CONFIG')}))\nprint('stderr-line',file=sys.stderr)\n")
            script.chmod(0o700)
            with patch.dict(os.environ,{"DOCKER_HOST":"tcp://evil:2375","DOCKER_CONTEXT":"evil","SECRET_TOKEN":"leak"}):
                raw = Docker(executable=str(script)).run(["ps","; touch not-a-command"],combine=True)
            first, second = raw.splitlines()
            data = json.loads(first)
            self.assertEqual(data["argv"],["--context","default","ps","; touch not-a-command"])
            self.assertIsNone(data["host"])
            self.assertIsNone(data["context"])
            self.assertEqual(second,"stderr-line")

    def test_large_or_hung_docker_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory)/"fake-docker"
            script.write_text("#!/usr/bin/python3\nprint('x'*100000)\n")
            script.chmod(0o700)
            with self.assertRaises(ApiError) as caught:
                Docker(executable=str(script)).run(["ps"],max_bytes=1024)
            self.assertEqual(caught.exception.code,"output_limit")
            script.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(10)\n")
            with self.assertRaises(ApiError) as caught:
                Docker(executable=str(script)).run(["ps"],timeout=.05)
            self.assertEqual(caught.exception.code,"timeout")

    def test_errors_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory)/"fake-docker"
            script.write_text("#!/usr/bin/python3\nimport sys\nprint('password=verysecret',file=sys.stderr)\nsys.exit(1)\n")
            script.chmod(0o700)
            with self.assertRaises(ApiError) as caught:
                Docker(executable=str(script)).run(["ps"])
            self.assertNotIn("verysecret",caught.exception.message)

    def test_inspect_is_closed_to_project_and_service_duplicates_fail(self):
        docker = Docker()
        row = {"id":"abc","project":PROJECT,"service":"gcs","state":"running","running":True,"health":None,"image_id":"sha256:fixed"}
        with patch.object(docker,"run",return_value="abc\n"),patch.object(docker,"_inspect",return_value=[row.copy()]):
            items = docker.containers()
        self.assertEqual(len(items),8)
        gcs = next(item for item in items if item["service"]=="gcs")
        self.assertEqual(gcs["health"],"not_defined")
        self.assertIsNone(gcs["version"])
        self.assertTrue(gcs["actions"]["restart"])
        self.assertFalse(next(item for item in items if item["service"]=="wireguard")["actions"]["restart"])
        with patch.object(docker,"run",return_value="abc\n"),patch.object(docker,"_inspect",return_value=[row.copy(),row.copy()]):
            with self.assertRaises(ApiError) as caught:
                docker.containers()
        self.assertEqual(caught.exception.code,"ambiguous_service")

    def test_restart_changes_only_selected_container_and_preserves_image(self):
        docker = Docker()
        before = {"id":"abc","service":"gcs","image_id":"sha256:fixed","state":"running","health":"not_defined"}
        with patch.object(docker,"resolve",side_effect=[before.copy(),before.copy()]),patch.object(docker,"run") as run:
            result = docker.action("gcs","restart")
            run.assert_called_once_with(["restart","--time","15","abc"],timeout=45)
        self.assertEqual(result["image_id"],"sha256:fixed")
        self.assertFalse(result["health_verified"])
        after = {**before,"image_id":"sha256:different"}
        with patch.object(docker,"resolve",side_effect=[before,after]),patch.object(docker,"run"):
            with self.assertRaises(ApiError) as caught:
                docker.action("gcs","restart")
        self.assertEqual(caught.exception.code,"container_changed")

    def test_network_guard_precedes_every_docker_command(self):
        docker = Docker()
        with patch.object(docker,"run") as run:
            for service in ("wireguard","net_setup","gcs_reactor"):
                with self.assertRaises(ApiError) as caught:
                    docker.action(service,"restart")
                self.assertEqual(caught.exception.code,"network_guard")
            run.assert_not_called()

    def test_portainer_link_uses_published_port(self):
        docker = Docker()
        values = ["abc\n",json.dumps({"9443/tcp":[{"HostIp":"127.0.0.1","HostPort":"19443"}]})]
        with patch.object(docker,"run",side_effect=values):
            self.assertEqual(docker.portainer()["url"],"https://127.0.0.1:19443")

    def test_portainer_restart_keeps_container_image_and_does_not_reset_accounts(self):
        docker=Docker()
        row={'image':'portainer/portainer-ce:2.45.2','image_id':'sha256:fixed','privileged':False,'mode':'bridge',
             'ports':{'9443/tcp':[{'HostIp':'127.0.0.1','HostPort':'9443'}]},'running':True}
        with patch.object(docker,'run',side_effect=['abc',json.dumps(row),'abc',json.dumps(row)]) as run:
            result=docker.restart_portainer()
        self.assertTrue(result['accounts_preserved'])
        self.assertIn(unittest.mock.call(['restart','--time','15','abc'],timeout=45),run.call_args_list)
        self.assertFalse(any('rm' in call.args[0] or 'volume' in call.args[0] for call in run.call_args_list))

    def test_portainer_restart_refuses_unrecognized_container_before_mutation(self):
        docker=Docker()
        for row in ({'image':'other/service:latest'}, {'image':'portainer/portainer-ce:2.45.2','privileged':True}):
            with patch.object(docker,'run',side_effect=['abc',json.dumps(row)]) as run:
                with self.assertRaises(ApiError) as caught:
                    docker.restart_portainer()
                self.assertEqual(caught.exception.code,'portainer_unmanaged')
                self.assertEqual(run.call_count,2)


if __name__=="__main__":
    unittest.main()
