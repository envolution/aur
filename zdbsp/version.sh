#!/bin/sh
curl -s https://zdoom.org/files/utils/zdbsp/ | grep -o 'zdbsp[^"]*src\.zip' | sort -uV | tail -n1