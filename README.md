# klp-build

The kernel livepatching creation tool

## Development

To install the project and dependencies use:

`pip install -e .`

To run the project locally and test your changes use:

`./klp-build`

To run tests use:

`tox -e tests`

## Settings
There are two environment variables that can be set before running the
klp-build commands.

### KLP_WORK_DIR
Required. This needs to point to a directory where the livepatch data will be
placed, including the data generated by the different stages of the livepatch
creation.

### KLP_DATA_DIR
Optional. This is the place where the source code is placed. To create a
livepatch for upstream kernel, this needs to point to a kernel tree with the
sources built.

Instead of setting this environment variables you can set --data-dir on the
setup phase of the livepatch creation.


# Creating a livepatch for upstream kernels - Not production ready yet

__IMPORTANT__: There are some still out-of-tree patches needed to make it to
work (klp-convert) tool. Check the patches directory for what is needed.

The current approach to create for upstream kernels needs a directory with the
source code, and the compiled sources in the same location.


## Setup

```sh
klp-build setup --kdir \
                --data-dir /home/mpdesouza/git/linux \
                --name sound_lp \
                --mod snd-pcm \
                --conf CONFIG_SND_PCM \
                --file-funcs sound/core/pcm.c snd_pcm_attach_substream
```

This command creates a new directory __$KLP_WORK_DIR__/sound_lp. The setup phase
checks if the configuration entry is set, and if the symbol being extracted is
present in the module.

Explaining some arguments:
--mod: The module to be livepatched. If empty, vmlinux will be livepatched
       instead.
--file-funcs: Lists the symbols (hence funcs) from each file. These
              symbols will be extracted into the livepatching.


## Extraction

For upstream kernels we only support using [clang-extract](https://github.com/SUSE/clang-extract)
for code extraction:
```sh
klp-build extract --name sound_lp
```

The contents of the generated file are placed
on __$KLP_WORK_DIR__/sound_lp/ce/__$codestream__/lp.


__IMPORTANT__: Do not use it on production. klp-build is still only used to
create livepatches on SLE kernels using klp-ccp. The tool needs more tests in
order to rely on this process to create livepatches for upstream kernels. More
work needs to be done before it happens, like:

* Generate a template to include and generate a compilable livepatch
* Simplify the setup/extraction in just one pass in order to make it even easier
  for the livepatch developer.
* Many other small adjustments


# Creating a livepatch for multiple SUSE Linux Enterprise codestreams


## Settings

Along with the environment variables mentioned earlier, we also need
KLP_KERNEL_SOURCE.

### KLP_KERNEL_SOURCE
Optional. This is only used for SLE kernels. This should contain the path to the
[kernel-source tree](https://github.com/SUSE/kernel-source) in order to check
which codestreams already contains the fix and don't need the livepatch. It also
gets the fix for the CVE being livepatched.

## Setup
To create a new "livepatch project", use the setup command:

```sh
klp-build setup --name bsc1197597 --cve 2022-1048 --mod snd-pcm --conf CONFIG_SND_PCM --file-funcs sound/core/pcm.c snd_pcm_attach_substream snd_pcm_detach_substream --codestreams '15.5' --archs x86_64 ppc64le
```

klp-build will check if the configuration is enabled, if the symbol is present
on the module being livepatched. The check will be done in all architectures
informed as argument. If the argument is not informed, it will return an error
if configuration is not available on any of them.


## Extraction

At this point we support two different backends to perform the code extraction:
[klp-ccp](https://github.com/SUSE/klp-ccp) and
[clang-extract](https://github.com/SUSE/clang-extract), but only klp-ccp is
being used in production. To extract the livepatches, run the command below:

```sh
klp-build extract --name bsc1197597 --type ccp
```

Depending of the __type__ chosen, it will use klp-ccp or clang-extract to
extract the livepatch from the sources. The resulting livepatched will be placed
on __$KLP_WORK_DIR__/__bsc1197597__/__ccp__/__$codestream__/lp, for example:

``/home/john/livepatches/bsc1197597/ccp/15.5u40/lp``
