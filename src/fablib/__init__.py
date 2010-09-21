#!/usr/bin/env python
# encoding: utf-8
"""
__init__.py

Created by Kurtiss Hare on 2010-08-15.
Copyright (c) 2010 Medium Entertainment, Inc. All rights reserved.
"""

import collections
import functools
import os
import tempfile
import time

from contextlib import contextmanager
from functools import partial, reduce
from fabric.api import abort, cd, env, sudo, put, run


def _(s, **kwargs):
    if hasattr(s, 'format') and hasattr(s.format, '__call__'):
        if kwargs:
            params = dict((k,v) for (k,v) in env.items() + kwargs.items())    
        else:
            params = env
        return s.format(**params)

    return s

def update_env(env, *items, **kwargs):
    unconditional = not kwargs.pop('conditional', True)
    for (key, value) in items:
        if getattr(env, key, None) is None or unconditional:
            if hasattr(value, '__call__'):
                value = value()
            setattr(env, key, _(value))
            
def install(helper, scope, prefix = None):
    names = (n for n in dir(helper) if not n.startswith('_'))

    if prefix is None:
        prefix = ''
    else:
        prefix = "{0}_".format(prefix)

    for name in names:
        scope["{0}{1}".format(prefix, name)] = getattr(helper, name)      


class ProjectHelper(object):
    def __init__(self, github_path = None, project_path = None, packages = None):
        self.github_path = github_path
        self._project_path = project_path
        self.packages = set(["git-core"])

        if packages:
            self.packages.update(set(packages))

    def include_base_environment(self):
        github_account, application = self.github_path.split("/")
        
        update_env(env,
            ('github_account',      github_account),
            ('application',         application),
            ('disable_known_hosts', True),
            ('keep_releases',       4),
            ('user',                "{application}-bot"),
            ('key_filename',        partial(self.root_path, "home/{user}/.ssh/id_rsa")),
            ('path',                "/var/www/{application}"),
            ('origin_uri',          "git@github.com:{github_account}/{application}.git"),
            ('shared_path',         "{path}/shared"),
            ('releases_path',       "{path}/releases"),
            ('current_path',        "{path}/current"),
            ('repository_path',     "{shared_path}/{application}")
        )

    def include_configure_environment(self):
        self.include_base_environment()

        update_env(env,
            ('_user',           env.user),
            ('user',            "ubuntu"),
            ('key_filename',    self.etc_path("{application}/ec2/{application}-keypair.pem"))
        )
    
    def include_prepare_environment(self):
        self.include_base_environment()
        # NOTE: no further customization at this time
        
    def include_deploy_environment(self, deploy_ref = "HEAD"):
        self.include_base_environment()

        update_env(env,
            ('deploy_ref',      deploy_ref),
            ('release_id',      time.strftime("%Y%m%d%H%M%S")),
            ('release_path',    "{releases_path}/{release_id}")
        )

    def configure(self):
        sudo(
            """
            DEBIAN_FRONTEND=noninteractive DEBIAN_PRIORITY=critical
            aptitude dist-upgrade -y &&
            apt-get update && 
            apt-get upgrade -y
            """,
            pty = True
        )

        self.addusers(env.user)

        put(
            self.root_path("etc/sudoers"),
            "/tmp/fabupload"
        )

        sudoers_output = sudo("""
            chown root:root /tmp/fabupload;
            chmod 440 /tmp/fabupload;
            visudo -c -f /tmp/fabupload;
            echo $?
            """
        )

        if sudoers_output.splitlines()[-1] != "0":
            sudo("rm -f /tmp/fabupload")
            abort("sudoers file ({0}) did not pass validation".format(self.root_path("etc/sudoers")))

        sudo("mv -f /tmp/fabupload /etc/sudoers")
        
    def prepare(self):
        self.install_packages()
        self.sudo("rm -rf {repository_path}")

        self.mkdirs(
            _("{path}"), 
            _("{shared_path}"), 
            _("{releases_path}"), 
        )

        self.run("git clone {origin_uri} {repository_path}")
        self.update()
        
    def deploy(self):
        self.update()
        self.clone()

        # symlink the new release
        self.run("""
            rm -f {current_path};
            ln -s {release_path} {current_path}
        """)

        # cleanup old releases
        with cd("{releases_path}".format(**env)):
            r = reversed(sudo("ls -dt */").splitlines())
            for dir in [d.strip('/') for d in r][:-env.keep_releases]:
                sudo("rm -rf {0}".format(dir))         
        
    def install_packages(self):
        if self.packages:
            package_list = " ".join(self.packages)

            self.sudo(_("""
                apt-get install -y
            """))
            
    def run(self, s, **kwargs):
        return run(_(s, **kwargs))

    def sudo(self, s, **kwargs):
        return sudo(_(s, **kwargs), pty = True)

    def install_packages(self):
        self.sudo("apt-get install -y {packages}",
            packages = " ".join(self.packages)
        )
        
    def update(self):
        self.run("""
            cd {repository_path};
            git pull origin master;
            git submodule init;
            git submodule update;
        """)

    def clone(self):
        # clone the ref to our new release path
        self.run("""
            git clone {repository_path} {release_path};
            cd {release_path};
            git checkout {deploy_ref};
            git submodule init;
            git submodule update;
        """)

    def mkdirs(self, *paths):
        for path in paths:
            self.sudo("""
                mkdir -p {path};
                chown {user}:{user} {path}
                """,
                path = path
            )

    def addusers(self, *users):
        for user in users:
            home_dir = _("home/{user}", user = user)
            ssh_dir = home_dir + "/.ssh"

            self.sudo("""
                if [  "`cat /etc/passwd | grep {user}:`" = "" ]; then useradd -m -d /{home_dir} -s /bin/bash {user}; fi
                """,
                home_dir = home_dir,
                user = user
            )

            self.sudo("""
                mkdir -p /{ssh_dir};
                chown {user}:{user} /{ssh_dir}
                """, 
                ssh_dir = ssh_dir,
                user = user
            )

            private_key_file = _("{ssh_dir}/id_rsa", ssh_dir = ssh_dir)
            public_key_file = _("{ssh_dir}/id_rsa.pub", ssh_dir = ssh_dir)
            ssh_config_file = _("{ssh_dir}/config", ssh_dir = ssh_dir)

            FileInfo = collections.namedtuple('FileInfo', 'local remote mode')

            layout = [
                FileInfo(self.root_path(private_key_file), "/" + private_key_file, 600),
                FileInfo(self.root_path(public_key_file), "/" + public_key_file, 640),
                FileInfo(self.root_path(public_key_file), _("/{ssh_dir}/authorized_keys", ssh_dir = ssh_dir), 640),
                FileInfo(self.root_path(ssh_config_file), "/" + ssh_config_file, 644)
            ]

            for info in layout:
                self.upload(info.local, info.remote, user, info.mode)

    def upload(self, local, remote, user, mode = 600):
        put(local, "/tmp/fabupload")
        self.sudo("""
            mv /tmp/fabupload {remote}
            chown {user}:{user} {remote}
            chmod {mode} {remote}
        """,
            user = user,
            mode = mode,
            remote = _(remote)
        )
        
    def render(self, local, remote, user, context = None, mode = 600):
        from jinja2 import Environment, FileSystemLoader
        je = Environment(loader = FileSystemLoader(os.path.dirname(local)))
        template = je.get_template(os.path.basename(local))
        result = template.render(context or dict())
        
        with tempfile.NamedTemporaryFile() as f:
            f.write(result)
            self.upload(f.name, remote, user, mode)

    def project_path(self, *paths):
        return _(os.path.join(self._project_path, *paths))
    
    def etc_path(self, *paths):
        return self.project_path("etc", *paths)
    
    def src_path(self, *paths):
        return self.project_path("src", *paths)
    
    def root_path(self, *paths):
        return self.etc_path("{application}/root", *paths)
        

class PythonProjectHelper(ProjectHelper):
    def __init__(self, *args, **kwargs):
        self.pip_cmd = kwargs.get('pip_cmd', 'pip')

        packages = set(["python", "python-dev", "python-virtualenv", "python-pip"])
        packages.update(kwargs.get('packages', set()))
        kwargs['packages'] = packages

        super(PythonProjectHelper, self).__init__(*args, **kwargs)
    
    def include_base_environment(self):
        super(PythonProjectHelper, self).include_base_environment()
        update_env(env,
            ('pip',     self.pip_cmd)
        )
    
    def include_deploy_environment(self):
        super(PythonProjectHelper, self).include_deploy_environment()
        update_env(env,
            ('requirements_path', "{release_path}/etc/pip/requirements.txt")
        )
    
    def pip(self, s, **kwargs):
        self.sudo("{{pip}} {0}".format(s), **kwargs)

    def install_packages(self):
        super(PythonProjectHelper, self).install_packages()
        self.pip("install virtualenvwrapper")
    
    def setup(self):
        super(PythonProjectHelper, self).setup()
        with self.virtualenvwrapper():
            pass

    def clone(self):
        super(PythonProjectHelper, self).clone()

        with self.virtualenvwrapper():
            self.run("""
                mkvirtualenv --no-site-packages {release_id};
                workon {release_id};
                easy_install pip;
                {pip} install -r {requirements_path};
                add2virtualenv {release_path}/src
            """)

    @contextmanager
    def virtualenvwrapper(self):
        self.run("""
            pushd;
            export WORKON_HOME={releases_path};
            source virtualenvwrapper.sh;
            cdvirtualenv
        """)

        try:
            yield
        finally:
            self.run("""
                deactivate;
                popd
            """)