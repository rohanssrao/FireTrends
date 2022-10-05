# FireTrends
Unofficial Chrome to Firefox patcher for a certain real or fictitious Chrome extension that may or may not display trends from your history in an unlimited fashion.

## Usage

Run `python3 patch.py`. It will ask for an extension ID which you can get from the last part of its URL on the Chrome Web Store. The ID will be a long string of letters.

## Temporary installation
*Works until the browser is restarted.*
1. Go to `about:debugging`
2. Click **This Firefox**
3. Click **Load Temporary Add-on...**
4. Select the `manifest.json` file in the patched folder

## Permanent installation
*Only do this if you are allowed to.*
1. Follow instructions here to sign the extension: https://stackoverflow.com/a/59172713
2. Drag the signed `.xpi` file into Firefox. It will be installed.

## Legal warning

Don't do anything illegal.
