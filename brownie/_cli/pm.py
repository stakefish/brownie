#!/usr/bin/python3

import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from tqdm import tqdm

from brownie import project
from brownie._config import _get_data_folder
from brownie.exceptions import InvalidPackage
from brownie.project.ethpm import get_manifest, install_package
from brownie.utils import color, notify
from brownie.utils.docopt import docopt

__doc__ = """Usage: brownie pm <command> [<arguments> ...] [options]

Commands:
  list                          List available accounts
  install <uri> [version]       Install a new package
  export <id> [path]            Copy an installed package into a new folder
  delete <id>                   Delete an installed package

Options:
  --help -h                     Display this message

Manager for ethPM or Github packages. Installed packages can be added
as dependencies and imported into your own projects.

See https://eth-brownie.readthedocs.io/en/stable/pm.html for more information.
"""


def main():
    args = docopt(__doc__)
    _get_data_folder().joinpath("packages").mkdir(exist_ok=True)
    try:
        fn = getattr(sys.modules[__name__], f"_{args['<command>']}")
    except AttributeError:
        print("Invalid command. Try brownie pm --help")
        return
    try:
        fn(*args["<arguments>"])
    except TypeError:
        print(f"Invalid arguments for command '{args['<command>']}'. Try brownie pm --help")
        return


def _list():
    org_names = []
    for path in _get_data_folder().joinpath("packages").iterdir():
        if not path.is_dir():
            continue
        if not list(path.iterdir()):
            path.unlink()
            continue
        org_names.append(path)

    if not org_names:
        print("No packages are currently installed.")
    else:
        print("The following packages are currently installed:")

    for org_path in org_names:
        packages = list(org_path.iterdir())
        print(f"\n{color('bright magenta')}{org_path.name}{color}")
        for path in packages:
            u = "\u2514" if path == packages[-1] else "\u251c"
            try:
                name, version = path.name.rsplit("@", maxsplit=1)
            except ValueError:
                continue
            print(f" {color('bright black')}{u}\u2500{_format_pkg(org_path.name, name, version)}")


def _export(package_id, path_str="."):
    org, repo, version = _split_id(package_id)
    source_path = _get_data_folder().joinpath(f"packages/{org}/{repo}@{version}")
    if not source_path.exists():
        raise FileNotFoundError(f"Package '{_format_pkg(org, repo, version)}' is not installed")
    dest_path = Path(path_str)
    if dest_path.exists():
        if not dest_path.is_dir():
            raise FileExistsError(f"Destination path already exists")
        dest_path = dest_path.joinpath(package_id)
    shutil.copytree(source_path, dest_path)
    notify("SUCCESS", f"Package '{_format_pkg(org, repo, version)}' was exported to {dest_path}")


def _delete(package_id):
    org, repo, version = _split_id(package_id)
    source_path = _get_data_folder().joinpath(f"packages/{org}/{repo}@{version}")
    if not source_path.exists():
        raise FileNotFoundError(f"Package '{_format_pkg(org, repo, version)}' is not installed")
    shutil.rmtree(source_path)
    notify("SUCCESS", f"Package '{_format_pkg(org, repo, version)}' has been deleted")


def _install(uri):
    if urlparse(uri).scheme in ("erc1319", "ethpm"):
        org, repo, version = _install_from_ethpm(uri)
    else:
        org, repo, version = _install_from_github(uri)

    notify("SUCCESS", f"Package '{_format_pkg(org, repo, version)}' has been installed")


def _install_from_ethpm(uri):
    manifest = get_manifest(uri)
    org = manifest["meta_brownie"]["registry_address"]
    repo = manifest["package_name"]
    version = manifest["version"]

    install_path = _get_data_folder().joinpath(f"packages/{org}")
    install_path.mkdir(exist_ok=True)
    install_path = install_path.joinpath(f"{repo}@{version}")
    if install_path.exists():
        raise FileExistsError("Package is aleady installed")

    try:
        project.new(install_path)
        install_package(install_path, uri)
        project.load(install_path)
    except Exception as e:
        shutil.rmtree(install_path)
        raise e

    return org, repo, version


def _install_from_github(package_id):
    org, repo, version = _split_id(package_id)

    data = requests.get(f"https://api.github.com/repos/{org}/{repo}/releases?per_page=100").json()
    org, repo = data[0]["html_url"].split("/")[3:5]
    releases = [i["tag_name"] for i in data]
    if version not in releases:
        raise ValueError(
            "Invalid version for this package. Available versions are:\n" + ", ".join(releases)
        ) from None

    install_path = _get_data_folder().joinpath(f"packages/{org}")
    install_path.mkdir(exist_ok=True)
    install_path = install_path.joinpath(f"{repo}@{version}")
    if install_path.exists():
        raise FileExistsError("Package is aleady installed")

    download_url = next(i["zipball_url"] for i in data if i["tag_name"] == version)
    response = requests.get(download_url, stream=True)
    total_size = int(response.headers.get("content-length", 0))
    progress_bar = tqdm(total=total_size, unit="iB", unit_scale=True)
    content = bytes()

    for data in response.iter_content(1024, decode_unicode=True):
        progress_bar.update(len(data))
        content += data
    progress_bar.close()

    existing = list(install_path.parent.iterdir())
    with zipfile.ZipFile(BytesIO(content)) as zf:
        zf.extractall(str(install_path.parent))
    installed = next(i for i in install_path.parent.iterdir() if i not in existing)
    shutil.move(installed, install_path)

    try:
        if not install_path.joinpath("contracts").exists():
            raise Exception
        project.new(install_path)
        project.load(install_path)
    except Exception:
        shutil.rmtree(install_path)
        raise InvalidPackage(f"{package_id} cannot be interpreted as a Brownie project")

    return org, repo, version


def _split_id(package_id):
    try:
        path, version = package_id.split("@")
        org, repo = path.split("/")
        return org, repo, version
    except ValueError:
        raise ValueError(
            "Invalid package ID. Must be given as [ORG]/[REPO]@[VERSION]"
            f"\ne.g. {_format_pkg('openzeppelin', 'openzeppelin-contracts', 'v2.5.0')}"
        ) from None


def _format_pkg(org, repo, version):
    return (
        f"{color('blue')}{org}/{color('bright blue')}{repo}"
        f"{color('blue')}@{color('bright blue')}{version}{color}"
    )
