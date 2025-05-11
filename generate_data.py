#!/usr/bin/env python3

import argparse
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib
import urllib.parse
from typing import Any

import requests

import pprint

URL = urllib.parse.urlparse("https://iommu.info/api")
# TODO: {'Authorization' : '', 'Accept' : 'application/json', 'Content-Type' : 'application/json'}
HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}
ERRORS: list[str] = []
DATAFILE = tempfile.NamedTemporaryFile(prefix="iommudb_", delete=False, mode="w+")


def _errors(problem: str, file: str | None = None, data: Any | None = None) -> None:
    """this is some pretty basic error handling"""

    ERRORS.append(problem)

    additional_text: str | None = None

    if data:
        additional_text = data

    if file and pathlib.Path(file).is_file():
        with pathlib.Path(file).open(encoding="utf-8") as fh:
            filedata = fh.read().strip()
        additional_text = "\n".join((f"cat {file}", filedata))

    if additional_text:
        ERRORS.append(additional_text)


def _exit() -> None:
    """exit printing any errors if present"""
    if ERRORS:
        # with open(DATAFILE.file.name) as fh:
        #    submission_data = fh.readlines()
        print(
            "please report this on github: https://github.com/mkoreneff/iommu_info_generate/issues/new/choose",
            "copy and paste the text below into the issue",
            "-".center(30, "-"),
            "\n".join(ERRORS),
            "-".center(30, "-"),
            f"you might also like to include the contents of {DATAFILE.file.name}",
            sep=os.linesep,
        )
        sys.exit(1)
    sys.exit(0)


def parse_hardware() -> dict[str, Any]:
    data_path = pathlib.Path("/sys/devices/virtual/dmi/id/")

    # setup some defaults
    # better to parse empty/unknown in the api serializer.. ?
    hardware: dict[str, Any] = {
        "board": {
            "name": "__unknown__",
            "board_vendor": {"name": "_none_", "vendorid": ""},
            "version": "__unknown__",
        },
        "bios": {
            "date": "",
            "release": "",
            "bios_vendor": {"name": "_none_", "vendorid": ""},
            "version": "__unknown__",
        },
        "chassis": {
            "type": "",
        },
        "product": {
            "family": "",
            "name": "",
        },
        "groups": [],
    }

    # parse the bios and board data
    for device in hardware:
        for key in hardware[device]:
            filepath: str = key if key.endswith("vendor") else f"{device}_{key}"
            filename = data_path / filepath
            if filename.is_file():
                with filename.open(encoding="utf-8") as f:
                    if data := f.read().strip():
                        if key == "date":
                            date = datetime.datetime.strptime(data, "%m/%d/%Y")
                            data = date.strftime("%Y-%m-%d")
                        if key.endswith("vendor"):
                            hardware[device][key]["name"] = data
                            continue
                        hardware[device][key] = data

    if hardware.get("chassis", {}).get("type", 0) in {"8", "9", "10"}:
        # update name for laptops and portable devices
        hardware["board"]["name"] = (
            f'{hardware["product"]["name"]} ' f'({hardware["product"]["family"]})'
        )
        hardware.pop("chassis")
        hardware.pop("product")

    return hardware


def parse_lspci_output(output: str, structure: dict[str, Any]):
    regex_id = re.compile(r"\[[\s\w]{4}\]")
    # slot and iommugroup in the main dict
    devices: dict[str, Any] = {"devices": [], "iommugroup": None}
    # devices in their own structure
    device = {}

    for line in output.split("\n"):
        if line:
            # split the line into key, value
            line = line.replace("\t", "").split(":", 1)
            # key to lower for json
            key: str = line[0].lower()
            # class translated to dev_class
            key = f"dev_{key}" if key == "class" else key
            value = line[1]
            data: dict[str, str | dict[str, str]] = {key: value}

            # handle the outside (group) and inside (device)
            if key.startswith("iommugroup"):
                devices.update(data)
                continue

            # if the value contains an ID, strip it out
            if len(value) > 6 and key.endswith("vendor"):
                if regex_id.match(value[-6:]):
                    dev_or_ven_id = value[-6:].strip("[]")
                    value = value[:-7]
                    # parse the vendor data separately
                    data = {key: {"name": value, "vendorid": dev_or_ven_id}}

            device.update(data)
        else:
            # there is a new line fed after the device details

            # hack to skip adding empty devices
            if not device:
                continue

            # if we don't know about the iommugroup, add it
            # otherwise append the devices
            if not any(
                [
                    g.get("iommugroup") == devices["iommugroup"]
                    for g in structure["groups"]
                ]
            ):
                # add the parsed device to the devices list
                devices["devices"].append(device)
                structure["groups"].append(devices)
            else:
                for group in structure["groups"]:
                    if group["iommugroup"] == devices["iommugroup"]:
                        group["devices"].append(device)

            # reset the device, and group after adding it to the structure
            devices: dict[str, Any] = {"devices": [], "iommugroup": None}
            device = {}

    return structure


def lookup_vendor(vendor_name: str, lookup_type: str) -> dict[str, str]:
    """Get pci-id vendorID"""
    # lookup the vendorid
    vendor: dict[str, str] = {}
    vendor_url = URL._replace(path="/api/vendor").geturl()
    params = {"vendor": vendor_name}
    r = requests.get(vendor_url, headers=HEADERS, params=params)
    if r.ok:
        if r.json().get("count") > 0:
            vendor = r.json().get("results")[0]
        else:
            _errors(
                problem=f"failed to retrieve {lookup_type} vendorid for: {vendor_name}",
                file=f"/sys/devices/virtual/dmi/id/{lookup_type}_vendor",
            )
    else:
        detail = r.json().get("detail", "")
        _errors(
            problem=f"Failed to retrieve {lookup_type}_vendor ({vendor_name}): {r.reason}, ({detail})"
        )
    return vendor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-f",
        "--file",
        help="accepts a file with the output of `lspci -nnvmm` instead of running a subprocess",
    )
    parser.add_argument(
        "-d",
        "--data",
        help="accepts a json file containing the system information data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="read local data, print to screen, don't upload to api",
    )
    args = parser.parse_args()

    if not args.data:
        hardware = parse_hardware()

    else:
        with open(args.data, "r") as fh:
            hardware = json.loads(fh.read())

    # lookup vendors from the database, stops writing bad vendors into DB
    board_vendor = hardware["board"]["board_vendor"]["name"]
    bios_vendor = hardware["bios"]["bios_vendor"]["name"]

    hardware["board"]["board_vendor"] = lookup_vendor(board_vendor, "board")
    hardware["bios"]["bios_vendor"] = lookup_vendor(bios_vendor, "bios")

    if not args.file:
        # get the output
        cmdline = [shutil.which("lspci"), "-nnvmm"]
        stdout = subprocess.run(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        groups = stdout.stdout
        output = parse_lspci_output(groups.decode(), hardware)
    else:
        with open(args.file, "r") as fh:
            output = parse_lspci_output(fh.read(), hardware)

    # persist data locally
    with DATAFILE as local_data:
        local_data.write(str(output))

    if args.dry_run:
        pprint.pprint(output)
        _exit()

    json_output = json.dumps(output)

    # POST the new data
    r = requests.post(URL.geturl(), data=json_output, headers=HEADERS)
    if r.ok:
        vendor = r.json().get("board", {}).get("board_vendor", {}).get("name")
        name = r.json().get("board", {}).get("name")
        query = {"board_vendor": vendor, "board_name": name}

        url = URL._replace(
            path="/mainboard", query=urllib.parse.urlencode(query)
        ).geturl()
        print(
            "Success, thanks for contributing to the project.",
            f"To view your submission online see {url}",
            f"To view the raw data submitted, please see {DATAFILE.file.name}",
            sep=os.linesep,
        )
    else:
        _errors(problem=f"API request error: {r.reason}", data=r.text)

    _exit()


if __name__ == "__main__":
    main()
