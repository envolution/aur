#!/bin/bash
 curl https://api.github.com/repos/sirjuddington/SLADE/releases/latest | grep -i "tag_name" | awk -F '"' '{print $4}'