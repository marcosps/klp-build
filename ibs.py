import concurrent.futures
import errno
import logging
from lxml import etree
from lxml.objectify import fromstring, SubElement
from natsort import natsorted
from operator import itemgetter
import os
from osctiny import Osc
from pathlib import Path
import re
import requests
import shutil
import subprocess
import sys
import time

from config import Config
from ksrc import GitHelper

class IBS(Config):
    def __init__(self, bsc, bsc_filter, working_cs = {}):
        super().__init__(bsc, bsc_filter, working_cs=working_cs)
        self.osc = Osc(url='https://api.suse.de')

        self.ibs_user = re.search('(\w+)@', self.email).group(1)
        self.prj_prefix = f'home:{self.ibs_user}:{self.bsc}-klp'

        self.kgraft_path = Path(Path().home(), 'kgr', 'kgraft-patches')
        if not self.kgraft_path.is_dir():
            raise RuntimeError('Couldn\'t find ~/kgr/kgraft-patches')

        self.kgraft_tests_path = Path(Path().home(), 'kgr',
                                      'kgraft-patches_testscripts')
        if not self.kgraft_tests_path.is_dir():
            raise RuntimeError('Couldn\'t find ~/kgr/kgraft-patches_testscripts')

        self.ksrc = GitHelper(self.bsc_num, self.filter)

        # Download all sources for x86
        # For ppc64le and s390x only download vmlinux and the built modules
        self.cs_data = {
                'ppc64le' : {
                    'kernel-default' : '(kernel-default-[\d\.\-]+.ppc64le.rpm)',
                },
                's390x' : {
                    'kernel-default' : '(kernel-default-[\d\.\-]+.s390x.rpm)',
                },
                'x86_64' : {
                    'kernel-default' : '(kernel-(default|rt)\-(extra|(livepatch|kgraft)?\-?devel)?\-?[\d\.\-]+.x86_64.rpm)',
                    'kernel-source' : '(kernel-(source|devel)(\-rt)?\-?[\d\.\-]+.noarch.rpm)'
                }
        }

        # Total number of work items
        self.total = 0

        # Skip osctiny INFO messages
        logging.getLogger('osctiny').setLevel(logging.WARNING)

    def do_work(self, func, args):
        if len(args) == 0:
            return

        with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
            results = executor.map(func, args)
            for result in results:
                if result:
                    logging.error(result)

    # The projects has different format: 12_5u5 instead of 12.5u5
    def get_projects(self):
        prjs = []
        projects = self.osc.search.project(f"starts-with(@name, '{self.prj_prefix}')")

        for prj in projects.findall('project'):
            prj_name = prj.get('name')
            cs = self.convert_prj_to_cs(prj_name)

            if self.filter and not re.match(self.filter, cs):
                continue

            prjs.append(prj)

        return prjs

    def get_project_names(self):
        names = []
        i = 1
        for result in self.get_projects():
            names.append((i, result.get('name')))
            i += 1

        return natsorted(names, key=itemgetter(1))

    def delete_project(self, i, prj, verbose=True):
        try:
            ret = self.osc.projects.delete(prj, force=True)
            if type(ret) is not bool:
                logging.error(etree.tostring(ret))
                raise ValueError(prj)
        except requests.exceptions.HTTPError as e:
            # project not found, no problem
            if e.response.status_code == 404:
                pass

        if verbose:
            logging.info(f'({i}/{self.total}) {prj} deleted')

    def delete_projects(self, prjs, verbose=True):
        for i, prj in prjs:
            self.delete_project(i, prj, verbose)

    def extract_rpms(self, args):
        i, cs, arch, rpm, dest = args
        if re.search('kernel\-(default|rt)\-\d+', rpm) or \
                re.search('kernel\-(default|rt)\-extra\-\d+', rpm):
            path_dest = self.get_data_dir(cs, arch)
        else:
            path_dest = self.get_data_dir(cs)

        rpm_file = Path(dest, rpm)
        path_dest.mkdir(exist_ok=True, parents=True)

        cmd = f'rpm2cpio {rpm_file} | cpio --quiet -uidm'
        subprocess.check_output(cmd, shell=True, cwd=path_dest)

        logging.info(f'({i}/{self.total}) extracted {cs} {rpm}: ok')

    def download_and_extract(self, args):
        i, cs, prj, repo, arch, pkg, rpm, dest = args

        self.download_binary_rpms(args)

        # Do not extract kernel-macros rpm
        if 'kernel-macros' not in rpm:
            self.extract_rpms( (i, cs, arch, rpm, dest) )

    def download_cs_data(self, cs_list):
        rpms = []
        extract = []
        i = 1

        logging.info('Getting list of files...')
        for cs, data in cs_list.items():
            prj = data['project']
            repo = data['repo']

            path_dest = Path(self.data, cs, 'kernel-rpms')
            path_dest.mkdir(exist_ok=True, parents=True)

            for arch, val in self.cs_data.items():
                if arch not in self.get_cs_archs(cs):
                    continue

                for k, regex in val.items():
                    pkg = k

                    # RT kernels have different package names
                    if self.cs_is_rt(cs):
                        if pkg == 'kernel-default':
                            pkg = 'kernel-rt'
                        elif pkg == 'kernel-source':
                            pkg = 'kernel-source-rt'

                    if repo != 'standard':
                        pkg = f'{pkg}.{repo}'

                    # arch is fixed for now
                    ret = self.osc.build.get_binary_list(prj, repo, arch, pkg)
                    for file in re.findall(regex, str(etree.tostring(ret))):
                        # FIXME: adjust the regex to only deal with strings
                        if isinstance(file, str):
                            rpm = file
                        else:
                            rpm = file[0]
                        rpms.append( (i, cs, prj, repo, arch, pkg, rpm, path_dest) )
                        i += 1

        logging.info(f'Downloading {len(rpms)} rpms...')
        self.total = len(rpms)
        self.do_work(self.download_and_extract, rpms)

        # Create a list of paths pointing to lib/modules for each downloaded
        # codestream
        for cs in cs_list:
            for arch in self.get_cs_archs(cs):
                mod_path= Path(self.get_data_dir(cs, arch), 'lib', 'modules')
                vmlinux_path = Path(self.get_data_dir(cs, arch), 'boot')

                for fext, ecmd in [('zst', 'unzstd --rm -f -d'), ('xz', 'xz --quiet -d')]:
                    cmd = f'find {mod_path} -name "*ko.{fext}" -exec {ecmd} --quiet {{}} \;'
                    subprocess.check_output(cmd, shell=True)

                subprocess.check_output(f'find {vmlinux_path} -name "vmlinux*gz" -exec gzip -d -f {{}} \;',
                                        shell=True)

            shutil.rmtree(Path(self.get_data_dir(cs), 'boot'), ignore_errors=True)
            shutil.rmtree(Path(self.get_data_dir(cs), 'lib'), ignore_errors=True)

            # Make sure that we have a proper config file for later executing of ccp
            odir = Path(f'{self.get_sdir(cs)}-obj', self.get_odir(cs))
            subprocess.check_output(['make', 'olddefconfig'], cwd=odir)

        logging.info('Finished extract vmlinux and modules...')

    def download_binary_rpms(self, args):
        i, cs, prj, repo, arch, pkg, rpm, dest = args

        try:
            self.osc.build.download_binary(prj, repo, arch, pkg, rpm, dest)
            logging.info(f'({i}/{self.total}) {cs} {rpm}: ok')
        except OSError as e:
            if e.errno == errno.EEXIST:
                logging.info(f'({i}/{self.total}) {cs} {rpm}: already downloaded. skipping.')
            else:
                raise RuntimeError(f'download error on {prj}: {rpm}')

    def convert_prj_to_cs(self, prj):
        return prj.replace(f'{self.prj_prefix}-', '').replace('_', '.')

    def apply_filter(self, item_list):
        if not self.filter:
            return item_list

        filtered = []
        for item in item_list:
            cmp_item = self.convert_prj_to_cs(item)
            if not re.match(self.filter, cmp_item):
                continue

            filtered.append(item)

        return filtered

    def find_missing_symbols(self, cs, arch, lp_mod_path):
        vmlinux_path = self.get_kernel_path(cs, arch)

        # Get list of UNDEFINED symbols from the livepatch module
        out = subprocess.check_output(['nm', '--undefined-only', str(lp_mod_path)],
                                      stderr=subprocess.STDOUT).decode()
        # Remove the U flag from every line
        lp_und_symbols = re.findall('\s+U\s([\w]+)', out)

        # vmlinux should have all symbols defined, but let's be safe here too
        vmlinux_syms = subprocess.check_output(['nm', '--defined-only', str(vmlinux_path)],
                                      stderr=subprocess.STDOUT).decode()

        missing_syms = []
        # Find all UNDEFINED symbols that exists in the livepatch module that
        # aren't defined in the vmlinux
        for sym in lp_und_symbols:
            if not re.search(f' {sym}', vmlinux_syms):
                missing_syms.append(sym)

        return missing_syms

    def validate_livepatch_module(self, cs, arch, rpm_dir, rpm):
        match = re.search('(livepatch)-.*(default|rt)\-(\d+)\-(\d+)\.(\d+)\.(\d+)\.', rpm)
        if match:
            dir_path = match.group(1)
            ktype = match.group(2)
            lp_file = f'livepatch-{match.group(3)}-{match.group(4)}_{match.group(5)}_{match.group(6)}.ko'
        else:
            ktype = 'default'
            match = re.search('(kgraft)\-patch\-.*default\-(\d+)\-(\d+)\.(\d+)\.', rpm)
            if match:
                dir_path = match.group(1)
                lp_file = f'kgraft-patch-{match.group(2)}-{match.group(3)}_{match.group(4)}.ko'

        fdest = Path(rpm_dir, rpm)
        # Extract the livepatch module for later inspection
        cmd = f'rpm2cpio {fdest} | cpio --quiet -uidm'
        subprocess.check_output(cmd, shell=True, cwd=rpm_dir)

        lp_mod_path = Path(rpm_dir, 'lib', 'modules',
                           f'{self.get_cs_kernel(cs)}-{ktype}',
                           dir_path, lp_file)
        out = subprocess.check_output(['/sbin/modinfo', str(lp_mod_path)],
                                      stderr=subprocess.STDOUT).decode()

        # Check depends field
        match = re.search('depends: (.+)', out)
        if match:
            deps = match.group(1).strip()
            # At this point we found that our livepatch module depends on
            # functions that are exported modules.

            # TODO: get the UND symbols from the livepatch and find which
            # symbols are not defined in the vmlinux. These symbols will need to
            # be worked in the livepatch.
            if deps:
                funcs = self.find_missing_symbols(cs, arch, lp_mod_path)
                logging.warning(f'{cs}:{arch} has dependencies: {deps}. Functions: {" ".join(funcs)}')

        shutil.rmtree(Path(rpm_dir, 'lib'), ignore_errors=True)

    # Parse 15.2u25 to SLE15-SP2_Update_25
    def get_full_cs(self, cs):
        sle, sp, up, rt = self.get_cs_tuple(cs)

        buf = f'SLE{sle}'

        if int(sp) > 0:
            buf = f'{buf}-SP{sp}'

        if rt:
            buf = f'{buf}-RT'

        return f'{buf}_Update_{up}'

    def prepare_tests(self, skip_download):
        if not skip_download:
            # Download all built rpms
            self.download()

        test_sh = Path(self.kgraft_tests_path,
                       f'{self.bsc}_test_script.sh')

        for arch in self.archs:
            tests_path = Path(self.bsc_path, 'tests', arch)
            test_arch_path = Path(tests_path, self.bsc)

            # Remove previously created directory and archive
            shutil.rmtree(test_arch_path, ignore_errors=True)
            shutil.rmtree(f'{str(test_arch_path)}.tar.xz', ignore_errors=True)

            test_arch_path.mkdir(exist_ok=True, parents=True)
            shutil.copy(Path(self.scripts, 'run-kgr-test.sh'),
                        test_arch_path)

            for d in ['built', 'repro', 'tests.out']:
                Path(test_arch_path, d).mkdir(exist_ok=True)

            build_cs = []
            for cs, data in self.filter_cs(verbose=False).items():
                if arch not in data['archs']:
                    continue

                rpm_dir = Path(self.bsc_path, 'c', cs, arch, 'rpm')
                if not rpm_dir.exists():
                    logging.info(f'{cs}/{arch}: rpm dir not found. Skipping.')
                    continue

                # TODO: there will be only one rpm, format it directly
                rpm = os.listdir(rpm_dir)
                if len(rpm) > 1:
                    raise RuntimeError(f'ERROR: {cs}/{arch}. {len(rpm)} rpms found. Excepting to find only one')

                for rpm in os.listdir(rpm_dir):
                    # Check for dependencies
                    self.validate_livepatch_module(cs, arch, rpm_dir, rpm)

                    shutil.copy(Path(rpm_dir, rpm), Path(test_arch_path, 'built'))

                if 'rt' in cs and arch != 'x86_64':
                    continue

                build_cs.append(self.get_full_cs(cs))

            # Prepare the config file used by kgr-test, use a set to remove
            # duplicated entries
            config = Path(test_arch_path, 'repro', f'{self.bsc}_config.in')
            with open(config, 'w') as f:
                f.write('\n'.join(build_cs))

            if test_sh.is_file():
                shutil.copy(test_sh, Path(test_arch_path, 'repro'))
            else:
                logging.warning(f'missing {test_sh}')

            subprocess.run(['tar', '-cJf', f'{self.bsc}.tar.xz',
                                f'{self.bsc}'], cwd=tests_path,
                                        stdout=sys.stdout,
                                        stderr=subprocess.PIPE, check=True)

    # We can try delete a project that was removed, so don't bother with errors
    def delete_rpms(self, cs):
        try:
            for arch in self.get_cs_archs(cs):
                shutil.rmtree(Path(self.bsc_path, 'c', cs, arch, 'rpm'),
                              ignore_errors=True)
        except KeyError:
            pass

    def download(self):
        rpms = []
        i = 1
        for result in self.get_projects():
            prj = result.get('name')
            cs = self.convert_prj_to_cs(prj)

            # Remove previously downloaded rpms
            self.delete_rpms(cs)

            archs = result.xpath('repository/arch')
            for arch in archs:
                ret = self.osc.build.get_binary_list(prj, 'devbuild', arch, 'klp')
                rpm_name = f'{arch}.rpm'
                for rpm in ret.xpath('binary/@filename'):
                    if not rpm.endswith(rpm_name):
                        continue

                    if 'preempt' in rpm:
                        continue

                    # Create a directory for each arch supported
                    dest = Path(self.bsc_path, 'c', cs, str(arch), 'rpm')
                    dest.mkdir(exist_ok=True, parents=True)

                    rpms.append( (i, prj, prj, 'devbuild', arch, 'klp', rpm, dest) )
                    i += 1

        logging.info(f'Downloading {len(rpms)} packages')
        self.total = len(rpms)
        self.do_work(self.download_binary_rpms, rpms)

    def status(self, wait=False):
        finished_prj = []
        while True:
            prjs = {}
            for _, prj in self.get_project_names():
                if prj in finished_prj:
                    continue

                prjs[prj] = {}

                for res in self.osc.build.get(prj).findall('result'):
                    code = res.xpath('status/@code')[0]
                    prjs[prj][res.get('arch')] = code

            for prj, archs in prjs.items():
                st = []
                # Check each arch build results and if all of them are finished,
                # add the prj to the finished_prj list to avoid showing it again
                # in the next round.
                finished = True
                for k, v in archs.items():
                    st.append(f'{k}: {v}')
                    if v not in ['succeeded', 'unresolvable', 'failed']:
                        finished = False

                if finished:
                    finished_prj.append(prj)

                logging.info('{}\t{}'.format(prj, '\t'.join(st)))

            if not wait or not prjs:
                break

            # Wait 30 seconds before getting status again
            time.sleep(30)
            logging.info('')

    def cleanup(self):
        prjs = self.get_project_names()

        self.total = len(prjs)
        if self.total == 0:
            logging.info('No projects found.')
            return

        logging.info(f'Deleting {self.total} projects...')

        self.delete_projects(prjs, True)

    def cs_to_project(self, cs):
        return self.prj_prefix + '-' + cs.replace('.', '_')

    def create_prj_meta(self, cs):
        data = self.get_cs_data(cs)

        prj = fromstring("<project name=''><title></title><description></description>" \
                "<build><enable/></build><publish><disable/></publish>" \
                "<debuginfo><disable/></debuginfo>" \
                "<repository name=\"devbuild\">" \
                f"<path project=\"{data['project']}\" repository=\"{data['repo']}\"/>" \
                "</repository>" \
                "</project>")

        repo = prj.find('repository')

        for arch in self.get_cs_archs(cs):
            ar = SubElement(repo, 'arch')
            ar._setText(arch)

        return prj

    def create_lp_package(self, i, cs):
        # get the kgraft branch related to this codestream
        branch = self.ksrc.get_cs_branch(cs)
        if not branch:
            logging.info(f'Could not find git branch for {cs}. Skipping.')
            return

        logging.info(f'({i}/{self.total}) pushing {cs} using branch {branch}...')

        # If the project exists, drop it first
        prj = self.cs_to_project(cs)
        self.delete_project(i, prj, verbose=False)

        meta = self.create_prj_meta(cs)
        prj_desc = f'Development of livepatches for {cs}'

        try:
            self.osc.projects.set_meta(prj, metafile=meta, title='',
                                       bugowner=self.ibs_user,
                                       maintainer=self.ibs_user,
                                       description=prj_desc)

            self.osc.packages.set_meta(prj, 'klp', title='', description='Test livepatch')

        except Exception as e:
            logging.error(e, e.response.content)
            raise RuntimeError('')

        base_path = Path(self.bsc_path, 'c', cs)

        # Remove previously created directories
        prj_path = Path(base_path, 'checkout')
        if prj_path.exists():
            shutil.rmtree(prj_path)

        code_path = Path(base_path, 'code')
        if code_path.exists():
            shutil.rmtree(code_path)

        self.osc.packages.checkout(prj, 'klp', prj_path)

        # Get the code from codestream
        subprocess.check_output(['/usr/bin/git', 'clone', '--single-branch',
                                 '-b', branch,
                                 str(self.kgraft_path), str(code_path)],
                                stderr=subprocess.STDOUT)

        # Fix RELEASE version
        with open(Path(code_path, 'scripts', 'release-version.sh'), 'w') as f:
            ver = self.get_full_cs(cs).replace('EMBARGO', '')
            f.write(f'RELEASE={ver}')

        # Check how to push multiple files
        # TODO: this isn't supported by osctiny YET.
        subprocess.check_output(['bash', './scripts/tar-up.sh', '-d', str(prj_path)],
                            stderr=subprocess.STDOUT, cwd=code_path)
        shutil.rmtree(code_path)

        subprocess.check_output(['osc', '-A', 'https://api.suse.de',
                                 'addremove'], stderr=subprocess.STDOUT,
                                cwd=prj_path)

        subprocess.check_output(['osc', '-A', 'https://api.suse.de', 'commit',
                                '-m', f'Dump {branch}'],
                                stderr=subprocess.STDOUT, cwd=prj_path)
        shutil.rmtree(prj_path)

        logging.info(f'({i}/{self.total}) {cs} done')

    def log(self, cs, arch):
        logging.info(self.osc.build.get_log(self.cs_to_project(cs), 'devbuild', arch,
                                     'klp'))

    def push(self, wait=False):
        cs_list = self.apply_filter(self.codestreams.keys())

        if not cs_list:
            raise RuntimeError(f'push: No codestreams found for {self.bsc}')

        logging.info(f'Preparing {len(cs_list)} projects on IBS...')

        self.total = len(cs_list)
        i = 1
        # More threads makes OBS to return error 500
        for cs in cs_list:
            self.create_lp_package(i, cs)
            i += 1

        if wait:
            # Give some time for IBS to start building the last pushed
            # codestreams
            time.sleep(30)
            self.status(wait)

            # One more status after everything finished, since we remove
            # finished builds on each iteration
            self.status(False)
