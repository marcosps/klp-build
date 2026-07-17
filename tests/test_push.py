# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 SUSE
# Author: Marcos Paulo de Souza

from pathlib import Path

import pytest

from klpbuild.plugins.push import validate_patched_objs


def test_missing_objs_in_patched_funcs(tmp_path, caplog):

    patched_text = "vmlinux bla klpp_bla"
    file1_text = """
        Any data...
        KLP_RELOC_SYMBOL(cifs, cifs, cifs_func)
    """

    file2_text = """
        Other data...
        KLP_RELOC_SYMBOL(vmlinux, vmlinux, func)
    """

    test_dir = Path(tmp_path, "check_mods")
    test_dir.mkdir()

    with open(test_dir / "patched_funcs.csv", "w") as f:
        f.write(patched_text)

    with open(test_dir / "livepatch1.c", "w") as f:
        f.write(file1_text)

    with open(test_dir / "livepatch2.c", "w") as f:
        f.write(file2_text)

    with pytest.raises(SystemExit):
        validate_patched_objs(test_dir)

    assert "Module cifs not found in KLP_RELOC_SYMBOLS. Please double check." in caplog.text

    caplog.clear()

    # Add the cifs module and make sure it passes
    patched_text += "\ncifs cifs_func klpp_cifs_funcs"
    with open(test_dir / "patched_funcs.csv", "w") as f:
        f.write(patched_text)

    validate_patched_objs(test_dir)
