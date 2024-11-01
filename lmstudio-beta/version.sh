#!/bin/bash
curl -L https://lmstudio.ai/beta-releases |\
     grep -o 'https://releases\.lmstudio\.ai/linux/x86/[0-9]\+.[0-9]\+.[0-9]\+/beta/[0-9]\+/LM_Studio-[0-9]\+.[0-9]\+.[0-9]\+.AppImage' |\
     sed 's|.*/x86/\([0-9]\.[0-9]\.[0-9]\)/beta.*|\1|'