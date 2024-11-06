#!/bin/bash
curl -s "https://aur.archlinux.org/rpc/v5/search/${AUR_MAINTAINER_NAME}?by=maintainer" | \
jq '{
    version: 2, 
    data: (.results | map({
	(.Name): {
	    version: (.Version | 
		gsub("^[0-9]+:"; "") |     # Remove epoch prefix (e.g., "2:")
		gsub("-[0-9]+$"; "")       # Extract version without release suffix
	    ),
	    release: (.Version | 
		capture("-(?<release>[0-9]+)$").release   # Capture release suffix as a separate value
	    )
	}
    }) | add)
}' > workingver.json      
jq 'del(.data[].release)' workingver.json > oldver.json
# Start the JSON file
echo '{ "version": 2, "data": {' > result.json

# Find all PKGBUILD files and process each one
mapfile -t pkgbuilds < <(find "${GITHUB_WORKSPACE}" -name 'PKGBUILD')
last_index=$((${#pkgbuilds[@]} - 1))

for i in "${!pkgbuilds[@]}"; do
  pkgbuild="${pkgbuilds[$i]}"
  
  # Source the PKGBUILD in a subshell to get the variables
  (
    source "$pkgbuild"
    # Check if pkgname, pkgver, and pkgrel are set
    if [[ -n "$pkgname" && -n "$pkgver" && -n "$pkgrel" ]]; then
      # Append the package info to the JSON file - some packages are in a group (like gnome-shell, so we force capture the 1st)
      echo -n "  \"${pkgname[0]}\": { \"pkgbuildversion\": \"$pkgver\",\"pkgbuildrel\": \"$pkgrel\"   }" >> result.json
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
jq -s '.[0] * .[1]' workingver.json result.json > combined.json
json_data=$(cat combined.json)

# Use jq to iterate over each package and compare fields
#echo "$json_data" | jq -r '
#  .data | to_entries[] | 
#  select(.value.version != .value.pkgbuildversion or .value.release != .value.pkgbuildrel) | 
#  .key
#'
echo "$json_data" | jq -r '
  .data | to_entries[] |
  select(
    (.value | has("version") and has("pkgbuildversion") and has("release") and has("pkgbuildrel")) and
    (.value.version != .value.pkgbuildversion or .value.release != .value.pkgbuildrel)
  ) |
  .key
'