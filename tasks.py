
import concurrent.futures
import signal
import sys
from tempfile import NamedTemporaryFile
from functools import partial
from pathlib import Path

from invoke import Collection, Executor, Failure, task, run
from invoke.util import cd
from invoke.vendor.yaml3 import dump

import colorama
colorama.init()
from termcolor import cprint, colored

import tasks_servers
import tasks_docker

try:
    # We import the tasks module of the applications via 'applications.XXX.tasks'; this extends the path so as to allow
    # them to find their companion modules (task_base, task_XXX). (Yes, this is abusing the import machinery a bit,
    # however, it works nicely and is rather simple).
    from applications import block, accounting
    sys.path[0:0] = block.__path__
    sys.path[0:0] = accounting.__path__

    from applications.block.tasks import deploy as deploy_block, try_load
    from applications.accounting.tasks import namespace as accounting
    from applications.drop.tasks import namespace as drop
    from applications.index.tasks import namespace as index
except ImportError:
    cprint('Applications are not up-to-date (inv scripts not found).\n'
           'Run "inv update" to fix.',
           'red', attrs=['bold'])
    deploy_block = try_load = lambda *args: True
    accounting = drop = index = {
        'deploy': lambda *args: None,
        'manage': lambda *args: None
    }
    HAVE_APPS = False
else:
    HAVE_APPS = True


APPS = {
    'applications/block': [
        'deploy',
     ],
    'applications/accounting': [
        'deploy',
        'manage -c "loaddata testdata.json"',
     ],
    'applications/drop': [
        'deploy',
     ],
    'applications/index': [
        'deploy',
     ],
}


def print_bold(*args, **kwargs):
    cprint(' '.join(args), attrs=['bold'], **kwargs)


def invoke_deploy_task(config_name, app, task):
    with cd(app):
        try:
            run('inv --config ' + config_name + ' ' + task, hide='both', pty=True)
        except Failure as failure:
            cprint('{app}: task "{task}" failed'.format_map(locals()), 'red')
            cprint('Error output is:', 'red')
            print(failure.result.stdout, end='')
            raise


@task(pre=[tasks_servers.start_all])
def deploy(ctx):
    def monitor_progress(futures, num_futures):
        mikado = ["._.", "._o", "o_O", "O_O", "O_o", "o_."]
        completed = 0
        while futures:
            for future in futures:
                status_update = 'Deploying ({m}/{n} complete)'.format(m=completed, n=num_futures)
                status_update += ' ' + mikado[0]
                mikado += mikado.pop(0),
                print(status_update, end='\r', flush=True)
                try:
                    future.result(0.1)
                except concurrent.futures.TimeoutError:
                    continue
                futures.remove(future)
                futures += future.continue_dependent()
                completed = num_futures - len(futures)
        print(' ' * len(status_update), end='\r')
        cprint('Deploying - done.', 'green', attrs=['bold'], flush=True)
    def submit(config_name, executor, app, tasks):
        if tasks:
            tasks = list(tasks)
            task = tasks.pop(0)
            future = executor.submit(invoke_deploy_task, config_name, app, task)
            future.continue_dependent = partial(submit, config_name, executor, app, tasks)
            return [future]
        return []
    with NamedTemporaryFile('w', suffix='.yaml') as config:
        # Dump current contexts' configuration into temporary YAML
        # file and use that as explicit runtime configuration for the
        # deployment tasks (which run in PPE worker processes).
        dump(ctx.config._collection, config)
        with concurrent.futures.ProcessPoolExecutor() as executor:
            futures = []
            total_number_of_tasks = 0
            for app in APPS:
                deploy_tasks = APPS[app]
                total_number_of_tasks += len(deploy_tasks)
                futures += submit(config.name, executor, app, deploy_tasks)
            monitor_progress(futures, total_number_of_tasks)


@task(
    pre=[deploy],
    help={
        'quiet': 'Smother uWSGI log output',
    },
)
def start(ctx, background=False, quiet=False):
    """
    Run server with uWSGI.

    Note: an explicit "stop" is only needed when run in the background (-b, --background)
          otherwise everything terminates on ^C (SIGINT).
    """
    pidfile = Path(ctx.qabel.testing.app_data) / 'uwsgi.pid'
    pidfile.parent.mkdir(exist_ok=True, parents=True)
    if tasks_servers.pidfile_alive(pidfile):
        print_bold('uWSGI is already running -- killable with "inv stop"')
        return False
    print_bold('Starting uWSGI')
    command_line = [
        'uwsgi',
        '--pidfile', pidfile,
        '--emperor', '"applications/*/deployed/current/uwsgi.ini"',
    ]
    if quiet:
        command_line.append('--logto /dev/null')
    if background:
        command_line.append('&')
    command_line = ' '.join(map(str, command_line))
    print_bold('uWSGI command line:')
    print_bold(command_line)
    run(command_line)
    return True


@task(post=[tasks_servers.stop_all])
def stop(ctx):
    pidfile = Path(ctx.qabel.testing.app_data) / 'uwsgi.pid'
    tasks_servers.kill_pidfile(pidfile, signal.SIGINT)


@task(pre=[tasks_servers.status])
def status(ctx):
    uwsgi_pidfile = Path(ctx.qabel.testing.app_data) / 'uwsgi.pid'
    if tasks_servers.pidfile_alive(uwsgi_pidfile):
        print('uWSGI is started, emperor PID', uwsgi_pidfile.read_text().strip())
    else:
        print('uWSGI is stopped')


@task(
    help={
        'pytest_args': 'Additional arguments passed to py.test',
        'which': 'Testing environment (see config). Default: adhoc.',
        'quiet': 'Smother uWSGI log output',
    }
)
def test(ctx, pytest_args='', which='adhoc', quiet=False):
    """
    Run the test suite against ad-hoc created infrastructure.
    """
    testenv = getattr(ctx.qabel.testing, which)
    start_servers = testenv.get('start_servers', False)
    pallin = Executor(namespace, ctx.config)
    if start_servers:
        # For correct resolution of pre/post tasks this is needed, a bit ugly but oh well.
        result = pallin.execute(
            ('start', {'background': True, 'quiet': quiet})
        )
        start_servers = result[start]  # only stop them if we actually had to start them
    command_line = ['py.test']
    for app in APPS:
        *_, app = app.split('/')
        app_url = '--{app}-url {url}'.format(app=app, url=testenv[app])
        command_line.append(app_url)
    command_line.append(pytest_args)
    command_line = ' '.join(command_line)
    print_bold(command_line)
    try:
        ctx.run(command_line, pty=True)
    finally:
        if start_servers:
            pallin.execute(('stop', {}))


@task
def update(ctx):
    """
    Update applications/* from git origin.
    """
    print_bold('Updating qabel-infrastructure')
    run('git pull --ff-only')
    for app in APPS:
        papp = Path(app)
        if not papp.exists():
            print_bold('Cloning', app)
            run('git clone https://github.com/Qabel/qabel-{name} {path}'.format(name=papp.name, path=papp))
            continue  # no need to pull if we just cloned
        with cd(app):
            print_bold('Updating', app)
            run('git pull --ff-only')


namespace = Collection(deploy, start, stop, status, test, update, tasks_servers.servers, tasks_docker.docker)
if not HAVE_APPS:
    namespace = Collection(update)

# Load configuration explicitly
for app in APPS:
    assert try_load(Path(app) / 'defaults.yaml', namespace)
assert try_load(Path(__file__).with_name('defaults.yaml'), namespace)
