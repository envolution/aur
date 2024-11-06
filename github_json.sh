#!/bin/bash

# Start the JSON file
echo '{ "version": 2, "data": {' > result.json

# Find all PKGBUILD files and process each one
pkgbuilds=($(find . -name 'PKGBUILD'))
last_index=$((${#pkgbuilds[@]} - 1))

for i in "${!pkgbuilds[@]}"; do
  pkgbuild="${pkgbuilds[$i]}"
  
  # Source the PKGBUILD in a subshell to get the variables
  (
    source "$pkgbuild"
    # Check if pkgname, pkgver, and pkgrel are set
    if [[ -n "$pkgname" && -n "$pkgver" && -n "$pkgrel" ]]; then
      # Append the package info to the JSON file
      echo -n "  \"$pkgname\": { \"pkgbuildversion\": \"$pkgver\",\"pkgbuildrel\": \"$pkgrel\"   }" >> result.json
      # Add a comma only if it's not the last item
      if [[ "$i" -ne "$last_index" ]]; then
        echo "," >> result.json
      fi
    fi
  )
done

# Close the JSON file
echo '  }' >> result.json
echo '}' >> result.json
jq -s '.[0] * .[1]' oldver.json result.json > combined.json
rm oldver.json result.json
mv combined.json oldver.json
json_data=$(cat oldver.json)

# Use jq to iterate over each package and compare fields
echo "$json_data" | jq -r '
  .data | to_entries[] | 
  select(.value.version != .value.pkgbuildversion or .value.release != .value.pkgbuildrel) | 
  .key
'
