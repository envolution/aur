#!/bin/bash

# Input TOML file
input_file="/tmp/12.toml"

# Process the TOML file
awk '
    /^\[.*\]$/ {
        # Extract the section name (remove the square brackets)
        section_name = substr($0, 2, length($0) - 2)

        # Set the output directory and file
        output_dir = "./" section_name
        output_file = output_dir "/.nvchecker.toml"

        # Create the directory
        system("mkdir -p \"" output_dir "\"")

        # Start writing to a new file
        current_file = output_file
        print $0 > current_file  # Write the section header
        next
    }
    {
        # Write the line to the current file
        if (current_file) {
            print $0 >> current_file
        }
    }
' "$input_file"
