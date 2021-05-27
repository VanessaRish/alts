# -*- mode:python; coding:utf-8; -*-
# author: Vasily Kleschov <vkleschov@cloudlinux.com>
# created: 2021-04-22

"""AlmaLinux Test System docker runner unit tests."""

from unittest import TestCase
from unittest.mock import MagicMock

from pathlib import Path

from ddt import ddt
from ddt import data, unpack
from mock import patch, Mock
# from pyfakefs.fake_filesystem_unittest import TestCase

from alts.worker.runners import DockerRunner

fedora_runner_params = ('test_id_1', 'fedora', '33')
centos_8_runner_params = ('test_id_2', 'centos', 8)
centos_7_runner_params = ('test_id_3', 'centos', 7)
ubuntu_runner_params = ('test_id_4', 'ubuntu', '20.04')
debian_runner_params = ('test_id_5', 'debian', '11.0')
almalinux_runner_params = ('test_id_6', 'almalinux', '8.3')

basics_data = (
    (
        centos_8_runner_params,
        {
            'ansible_connection_type': 'docker',
            'repositories': [],
            'pkg_manager': 'dnf'
        }
    ),
    (
        centos_7_runner_params,
        {
            'ansible_connection_type': 'docker',
            'repositories': [],
            'pkg_manager': 'yum'
        }
    ),
    (
        ubuntu_runner_params,
        {
            'ansible_connection_type': 'docker',
            'repositories': [],
            'pkg_manager': 'apt-get'
        }
    ),
    (
        fedora_runner_params,
        {
            'ansible_connection_type': 'docker',
            'repositories': [],
            'pkg_manager': 'dnf'
        }
    ),
    (
        debian_runner_params,
        {
            'ansible_connection_type': 'docker',
            'repositories': [],
            'pkg_manager': 'apt-get'
        }
    ),
    (
        almalinux_runner_params,
        {
            'ansible_connection_type': 'docker',
            'repositories': [],
            'pkg_manager': 'dnf'
        }
    ),
)

exec_data = (
    ('yum', 'update'),
    ('yum', 'install', '-y', 'python3')
)


@ddt
class TestDockerRunner(TestCase):

    work_dir = Path('/some/test/dir')
    artifacts_dir = work_dir / 'artifacts'

    @data(*basics_data)
    @unpack
    def test_basics(self, inputs: tuple, expected: dict):
        runner = DockerRunner(*inputs)
        self.assertIsInstance(runner.dist_name, str)
        self.assertIsInstance(runner.dist_version, str)
        for attribute in ('ansible_connection_type', 'repositories',
                          'pkg_manager'):
            self.assertEqual(getattr(runner, attribute), expected[attribute])

    # def setUp(self) -> None:
    #     self.patcher = patch('os.stat', MagicMock())
    #     self.another_patcher = patch('os.path.exists', MagicMock())
    #     self.patcher.start()
    #     self.another_patcher.start()
    #
    # def tearDown(self) -> None:
    #     self.patcher.stop()
    #     self.another_patcher.stop()

    @data(*basics_data)
    @unpack
    @patch('alts.worker.runners.base.tempfile.mkdtemp',
           MagicMock(return_value=work_dir))
    def test_working_directory_creation(self, inputs: tuple, expected: dict):
        runner = DockerRunner(*inputs)
        runner._create_work_dir()
        self.assertEqual(runner._work_dir, self.work_dir)

    @data(*basics_data)
    @unpack
    @patch('alts.worker.runners.base.tempfile.mkdtemp',
           MagicMock(return_value=artifacts_dir))
    @patch('alts.worker.runners.base.os.mkdir', MagicMock())
    def test_artifacts_directory_creation(self, inputs: tuple, expected: dict):
        runner = DockerRunner(*centos_8_runner_params)
        runner._create_artifacts_dir()
        self.assertEqual(runner._work_dir, self.artifacts_dir)

    @patch('alts.worker.runners.base.tempfile.mkdtemp',
           MagicMock())
    @patch('alts.worker.runners.docker.os.path.join', MagicMock())
    def test_error_render_tf_main_file(self):
        wrong_params = ('test_id_test', 'test_name', 'test', [], 'test_arch')
        runner = DockerRunner(*wrong_params)
        with self.assertRaises(ValueError):
            runner._render_tf_main_file()

    @data(*basics_data)
    @unpack
    @patch('alts.worker.runners.docker.local')
    def test_exec(self, inputs: tuple, expected: dict, mocked_func):
        true_arguments = {
            'args': ('exec', f'docker_{inputs[0]}', 'test'),
            'retcode': None,
            'cwd': None
        }
        runner = DockerRunner(*inputs)
        runner._exec(['test'])
        self.assertEqual(mocked_func.__getitem__.call_count, 1)
        self.assertEqual(mocked_func.__getitem__.call_args.args, ('docker',))
        self.assertEqual(mocked_func.__getitem__().run.call_count, 1)
        self.assertEqual(mocked_func.__getitem__().run.call_args.kwargs,
                         true_arguments)

