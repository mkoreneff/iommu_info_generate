#!/usr/bin/env python3

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess

import requests

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

    return hardware


def parse_lspci_output(output, structure):
    regex_id = re.compile("\[[\s\w]{4}\]")
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
    args = parser.parse_args()
    # TODO: add errors and print at the end
    errors = []

    if not args.data:
        hardware = parse_hardware()
        board_vendor = hardware["board"]["board_vendor"]["name"]
        bios_vendor = hardware["bios"]["bios_vendor"]["name"]
        # lookup the vendorid
        vendor_url = f"{URL}vendor/"
        lookup_url = f'{vendor_url}{board_vendor}'
        r = requests.get(lookup_url, headers=HEADERS)
        if r.ok:
            try:
                vendorid = json.loads(r.text)[0].get("vendorid")
                hardware["board"]["board_vendor"]["vendorid"] = vendorid
            except IndexError as e:
                print(
                    f'{e} while trying to lookup Mainboard vendorid for: {board_vendor}',
                    "Please report this problem and supply the output of",
                    "cat /sys/devices/virtual/dmi/id/board_vendor",
                    sep=os.linesep,
                )
                hardware["board"]["board_vendor"]["vendorid"] = ""
        else:
            print(f"Mainbaord vendor ({board_vendor}): {r.reason} in database. Please report this.")

        lookup_url = f'{vendor_url}{bios_vendor}'
        r = requests.get(lookup_url, headers=HEADERS)
        if r.ok:
            try:
                vendorid = json.loads(r.text)[0].get("vendorid")
                hardware["bios"]["bios_vendor"]["vendorid"] = vendorid
            except IndexError as e:
                print(
                    f'{e} while trying to lookup Bios vendorid for: {bios_vendor}',
                    "Please report this problem and supply the output of",
                    "cat /sys/devices/virtual/dmi/id/bios_vendor",
                    sep=os.linesep,
                )
                hardware["bios"]["bios_vendor"]["vendorid"] = ""
        else:
            print(
                f'Bios vendor ({bios_vendor}): {r.reason} in database. Please report this.'
            )

    else:
        with open(args.data, "r") as fh:
            hardware = json.loads(fh.read())

    if not args.file:
        # get the output
        lspci = shutil.which("lspci")
        cmdline = [f"{lspci}", "-nnvmm"]
        stdout = subprocess.run(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        groups = stdout.stdout
        output = parse_lspci_output(groups.decode(), hardware)
    else:
        with open(args.file, "r") as fh:
            output = parse_lspci_output(fh.read(), hardware)

    json_output = json.dumps(output)

    # POST the new data
    r = requests.post(URL, data=json_output, headers=HEADERS)
    if r.ok:
       print("Success, thanks for contributing to the project.")
    else:
       print(
           f"request error: {r.reason}",
           f"{r.text}",
           "please report this issue: https://github.com/mkoreneff/iommu_info_generate/issues/new/choose",
           sep=os.linesep
       )


if __name__ == "__main__":
    main()
