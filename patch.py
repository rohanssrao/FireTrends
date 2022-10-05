import os
import subprocess
import zipfile
import urllib.request
import shutil


ext_id = input("Enter a Chrome extension id: ")

crx_url = f"https://clients2.google.com/service/update2/crx?response=redirect&prodversion=31.0.1609.0&acceptformat=crx2,crx3&x=id%3D{ext_id}%26uc"

urllib.request.urlretrieve(crx_url, "original.crx")

with zipfile.ZipFile("original.crx", "r") as zip_ref:
    zip_ref.extractall("original")

subprocess.run(
    "python3 lib/xdelta3-dir-patcher.py apply --ignore-euid original patch patched",
    shell=True,
)

os.remove("original.crx")
shutil.rmtree("original")

print("------------------")
print("Patching complete. Patched extension is in the patched folder.")
