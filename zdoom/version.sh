#!/bin/bash
sourcesite="https://zdoom.org/files/zdoom/"
major=$(curl -s $sourcesite | grep -o 'title="[0-9]\+\.[0-9]\+"' | grep -o '[0-9]\+\.[0-9]\+' | sort -V | tail -n1)
curl -s $sourcesite/$major/ | grep -o 'zdoom-[^"]*-src' | sort -uV | tail -n1 | grep -oP '[0-9]+(\.[0-9]+)*'
