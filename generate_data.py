#!/usr/bin/env python3

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

import requests

import pprint

URL = "https://iommu.info/api/"
# TODO: {'Authorization' : '', 'Accept' : 'application/json', 'Content-Type' : 'application/json'}
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def parse_hardware():
    data_path = "/sys/devices/virtual/dmi/id/"

    # setup some defaults
    # better to parse empty/unknown in the api serializer.. ?
    hardware = {
        "board": {
            "name": "__unknown__",
            "board_vendor": {"name": "_none_", "vendorid": ""},
            "version": "__unknown__",
        },
        "bios": {
            "date": "",
            "release": "",
            "bios_vendor": {"name": "_none_", "vendorid": ""},
            "version": "",
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
            filepath = key if key.endswith("vendor") else f"{device}_{key}"
            filename = os.path.join(data_path, filepath)
            if os.path.exists(filename):
                with open(filename) as f:
                    data = f.read().strip()
                    if key == "date":
                        date = datetime.datetime.strptime(data, "%m/%d/%Y")
                        data = date.strftime("%Y-%m-%d")
                    if key.endswith("vendor"):
                        hardware[device][key]["name"] = data
                        continue
                    hardware[device][key] = data

    if hardware.get("board", {}).get("version", "") == "":
        print("board version blank, setting '__unknown__'")
        hardware["board"]["version"] = "__unknown__"

    if hardware.get("chassis", {}).get("type", 0) in {"8", "9", "10"}:
        hardware["board"]["name"] = (
            f'{hardware["product"]["name"]} ' f'({hardware["product"]["family"]})'
        )
        hardware.pop("chassis")
        hardware.pop("product")

    return hardware


def parse_lspci_output(output, structure):
    regex_id = re.compile(r"\[[\s\w]{4}\]")
    # slot and iommugroup in the main dict
    devices = {"devices": [], "iommugroup": None}
    # devices in their own structure
    device = {}

    for line in output.split("\n"):
        if line:
            # split the line into key, value
            line = line.replace("\t", "").split(":", 1)
            # key to lower for json
            key = line[0].lower()
            # class translated to dev_class
            key = f"dev_{key}" if key == "class" else key
            value = line[1]
            data = {key: value}

            # handle the outside (group) and inside (device)
            if key.startswith("iommugroup"):
                devices.update(data)
                continue

            # if the value contains an ID, strip it out
            if len(value) > 6:
                if regex_id.match(value[-6:]):
                    dev_or_ven_id = value[-6:].strip("[]")
                    value = value[:-7]

            # parse the vendor data separately
            if key.endswith("vendor"):
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
            devices = {"devices": [], "iommugroup": None}
            device = {}

    return structure


def lookup_vendor_id(vendor_name, lookup_type):
    """Get pci-id vendorID"""
    # lookup the vendorid
    vendorid = ""
    vendor_url = f"{URL}vendor/"
    params = {"vendor": vendor_name}
    r = requests.get(vendor_url, headers=HEADERS, params=params)
    if r.ok:
        try:
            vendorid = r.json().get("results")[0].get("vendorid")
        except IndexError as e:
            print(
                f"{e} while trying to lookup vendorid for: {vendor_name}",
                "Please report this problem and supply the output of",
                f"cat /sys/devices/virtual/dmi/id/{lookup_type}",
                "https://github.com/mkoreneff/iommu_info_generate/issues/new/choose",
                sep=os.linesep,
            )
    else:
        print(
            f"{lookup_type} ({vendor_name}): {r.reason} in database. Please report this",
            "https://github.com/mkoreneff/iommu_info_generate/issues/new/choose",
        )
    return vendorid


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

        # lookup vendor ids from the database
        board_vendor = hardware["board"]["board_vendor"]["name"]
        bios_vendor = hardware["bios"]["bios_vendor"]["name"]

        hardware["board"]["board_vendor"]["vendorid"] = lookup_vendor_id(
            board_vendor, "board_vendor"
        )
        hardware["bios"]["bios_vendor"]["vendorid"] = lookup_vendor_id(
            bios_vendor, "bios_vendor"
        )

    else:
        with open(args.data, "r") as fh:
            hardware = json.loads(fh.read())

    if not args.file:
        # get the output
        cmdline = [shutil.which("lspci"), "-nnvmm"]
        stdout = subprocess.run(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        groups = stdout.stdout
        output = parse_lspci_output(groups.decode(), hardware)
    else:
        with open(args.file, "r") as fh:
            output = parse_lspci_output(fh.read(), hardware)

    if args.dry_run:
        pprint.pprint(output)
        sys.exit(0)

    json_output = json.dumps(output)

    # POST the new data
    r = requests.post(URL, data=json_output, headers=HEADERS)
    if r.ok:
        vendor = r.json().get("board", {}).get("board_vendor", {}).get("name")
        name = r.json().get("board", {}).get("name")
        print(
            "Success, thanks for contributing to the project.",
            f"To view your submission see https://iommu.info/mainboard/{vendor}/{name}",
            sep=os.linesep,
        )
    else:
        print(
            f"request error: {r.reason}",
            f"{r.text}",
            "please report this issue: https://github.com/mkoreneff/iommu_info_generate/issues/new/choose",
            sep=os.linesep,
        )


if __name__ == "__main__":
    main()
