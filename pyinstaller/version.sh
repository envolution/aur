#!/bin/bash
curl https://api.github.com/repos/pyinstaller/pyinstaller/releases/latest | grep -i "tag_name" | awk -F '"' '{print $4}' | cut -c2-^C