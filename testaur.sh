#!/bin/bash

# Function to check SSH key
check_ssh_key() {
    echo "Checking for SSH keys..."
    if [ -f "$HOME/.ssh/id_rsa" ]; then
        echo "SSH key found at $HOME/.ssh/id_rsa."
    else
        echo "No SSH key found. Please generate an SSH key."
        exit 1
    fi
}

# Function to check known_hosts
check_known_hosts() {
    HOST=$1
    echo "Checking known_hosts for $HOST..."
    if ssh-keygen -F "$HOST" > /dev/null; then
        echo "Host key for $HOST found in known_hosts."
    else
        echo "No host key found for $HOST. Consider adding it."
        echo "You can connect manually to add it: ssh $HOST"
    fi
}

# Function to check SSH connection
check_ssh_connection() {
    HOST=$1
    echo "Testing SSH connection to $HOST..."
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" exit
    if [ $? -eq 0 ]; then
        echo "SSH connection to $HOST is successful."
    else
        echo "SSH connection to $HOST failed. Check your network and firewall settings."
    fi
}

# Main script
echo "AUR Diagnostics Script"

# Check for SSH key
check_ssh_key

# Specify the AUR host (replace with the correct host if needed)
AUR_HOST="aur@aur.archlinux.org"

# Check known_hosts
check_known_hosts "$AUR_HOST"

# Check SSH connection
check_ssh_connection "$AUR_HOST"

echo "Diagnostics complete."
