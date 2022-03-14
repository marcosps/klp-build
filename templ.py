from datetime import datetime
import git
import jinja2
import json
import pathlib
import os
import re
import requests
import textwrap

class Template:
    def __init__(self, bsc, work_dir, ktype):
        self._bsc = 'bsc' + str(bsc)
        conf = pathlib.Path(work_dir, self._bsc, 'conf.json')
        if not conf.is_file():
            raise ValueError('config.json not found in {}'.format(str(conf)))

        self._ktype = ktype
        with open(conf, 'r') as f:
            data = json.load(f)
            self._bsc_num = data['bsc']
            self._mod = data['mod']
            self._cve = data['cve']
            self._conf = data['conf']
            self._commits = data['commits']
        try:
            conf = git.GitConfigParser()
            self._user = conf.get_value('user', 'name')
            self._email = conf.get_value('user', 'email')
        except:
            raise RuntimeError('Please define name/email in global git config')

        fsloader = jinja2.FileSystemLoader(pathlib.Path(os.path.dirname(__file__), 'templates'))
        self._env = jinja2.Environment(loader=fsloader, trim_blocks=True)

    def GenerateLivePatches(self):
        fname = 'kgr_patch' if self._ktype == 'kgr' else 'livepatch'
        fname = fname + '_' + self._bsc

        bsc = pathlib.Path(self._bsc)
        bsc.mkdir(exist_ok=True)

        for ext in ['h', 'c']:
            templ = self._env.get_template('lp-' + ext + '.j2')

            templ.globals['year'] = datetime.today().year

            if self._mod:
                templ.globals['mod'] = self._mod

            lp_file = pathlib.Path(bsc, fname + '.' + ext)
            with open(lp_file, 'w') as f:
                f.write(templ.render(bsc = self._bsc,
                                    bsc_num = self._bsc_num,
                                    cve = self._cve,
                                    config = self._conf,
                                    ktype = self._ktype,
                                    user = self._user,
                                    email = self._email,
                                    commits = self._commits))

    # Return the commit message in a list of wrapped
    def generate_commit_msg(self):
        templ = self._env.get_template('commit.j2')
        return templ.render(bsc = self._bsc,
                            bsc_num = self._bsc_num,
                            cve = self._cve,
                            user = self._user,
                            email = self._email,
                            commits = self._commits)
