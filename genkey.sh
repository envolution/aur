#!/bin/bash

# Default email
DEFAULT_EMAIL="your_email@example.com"  # Change this to your preferred default email
EMAIL=${1:-$DEFAULT_EMAIL}  # Capture email from argument or use default

# Variables
KEY_PATH="$HOME/.ssh/id_rsa"

# Function to check if the SSH key already exists
check_existing_key() {
    if [ -f "$KEY_PATH" ]; then
        echo "SSH key already exists at $KEY_PATH."
        exit 0
    fi
}

# Function to generate the SSH key
generate_ssh_key() {
    echo "Generating SSH key..."
    ssh-keygen -t rsa -b 4096 -C "$EMAIL" -f "$KEY_PATH" -N ""
    echo "SSH key generated at $KEY_PATH."
}

# Function to add the SSH key to the SSH agent
add_ssh_key_to_agent() {
    eval "$(ssh-agent -s)"  # Start the SSH agent
    ssh-add "$KEY_PATH"
    echo "SSH key added to the SSH agent."
}

# Function to output the public key
output_public_key() {
    echo "Public key:"
    cat "${KEY_PATH}.pub"
    echo "Please add this public key to your AUR account."
}

# Main script execution
check_existing_key
generate_ssh_key
add_ssh_key_to_agent
output_public_key

echo "Setup complete."
