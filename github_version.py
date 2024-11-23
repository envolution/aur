#!/usr/bin/env python3

import argparse
import os
import sys
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple
import requests
from urllib.parse import urlparse, parse_qs, urljoin

@dataclass
class VersionInfo:
    commit_sha: str
    commit_count: int
    version_tag: Optional[str] = None

    def __str__(self) -> str:
        if self.version_tag:
            return f"{self.version_tag}+r{self.commit_count}+g{self.commit_sha}"
        return f"r{self.commit_count}+g{self.commit_sha}"

class GitHubError(Exception):
    """Base exception for GitHub API errors."""
    pass

class GitHubRateLimitError(GitHubError):
    def __init__(self, reset_time: int):
        self.reset_time = reset_time
        self.wait_seconds = reset_time - int(datetime.now().timestamp())
        super().__init__(f"Rate limit exceeded. Reset in {self.wait_seconds} seconds.")

class GitHubNotFoundError(GitHubError):
    def __init__(self, resource: str):
        self.resource = resource
        super().__init__(f"Resource not found: {resource}")

class GitHubAPI:
    BASE_URL = "https://api.github.com"

    def __init__(self, owner_repo: str, token: Optional[str] = None):
        try:
            self.owner, self.repo = owner_repo.split("/")
        except ValueError:
            raise GitHubError("Invalid format for owner/repo. Expected 'owner/repo'.")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "github-version-script"
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _get_api_url(self, endpoint: str) -> str:
        """Construct full API URL."""
        if not endpoint:  # Handle null or empty endpoint
            return urljoin(self.BASE_URL, f"/repos/{self.owner}/{self.repo}")
        return urljoin(self.BASE_URL, f"/repos/{self.owner}/{self.repo}/{endpoint.lstrip('/')}")

    def _make_request(self, endpoint: str) -> dict:
        """Make a GitHub API request with comprehensive error handling."""
        url = self._get_api_url(endpoint)
        try:
            response = self.session.get(url)
            
            if response.status_code == 403 and int(response.headers.get("X-RateLimit-Remaining", 1)) == 0:
                raise GitHubRateLimitError(int(response.headers.get("X-RateLimit-Reset", 0)))
            if response.status_code == 404:
                raise GitHubNotFoundError(url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            raise GitHubError(f"Request failed: {e}")

    def get_repo_info(self) -> str:
        """Get repository default branch."""
        info = self._make_request("")
        branch = info.get("default_branch")
        if not branch:
            raise GitHubError("Could not determine default branch.")
        return branch

    def get_commit_info(self, branch: str) -> Tuple[str, int]:
        """Get latest commit SHA and commit count."""
        commit_info = self._make_request(f"commits/{branch}")
        sha = commit_info.get("sha", "")[:9]
        if not sha:
            raise GitHubError("Could not retrieve commit SHA.")
        
        commits_response = self.session.get(
            self._get_api_url("commits"),
            params={"per_page": "1", "sha": branch}
        )
        commit_count = 1
        if 'Link' in commits_response.headers:
            link_header = commits_response.headers['Link']
            for link in link_header.split(", "):
                if 'rel="last"' in link:
                    url = link[link.find("<") + 1:link.find(">")]
                    query_params = parse_qs(urlparse(url).query)
                    commit_count = int(query_params['page'][0]) if 'page' in query_params else None
                    break
        return sha, commit_count

    def get_latest_version(self, use_tags: bool) -> Optional[str]:
        """Get the latest version from tags or releases."""
        endpoint = "tags" if use_tags else "releases/latest"
        try:
            data = self._make_request(endpoint)
            if use_tags:
                return data[0]["name"] if data else None
            return data.get("tag_name")
        except GitHubNotFoundError:
            return None

def main():
    parser = argparse.ArgumentParser(description="Get version information from GitHub repository.")
    parser.add_argument("owner_repo", help="Repository in 'owner/repo' format.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tags", action="store_true", help="Use tags for versioning.")
    group.add_argument("--releases", action="store_true", help="Use releases for versioning.")
    group.add_argument("--commits", action="store_true", help="Use only the number of commits and commit SHA.")
    parser.add_argument("-strip", help="String to strip from version tags.")
    parser.add_argument("-regex", help="Filter output using a regex pattern.")
    args = parser.parse_args()

    if not args.tags and not args.releases and not args.commits:
        args.releases = True

    try:
        github = GitHubAPI(args.owner_repo, token=os.getenv("GITHUB_TOKEN"))

        branch = github.get_repo_info()
        commit_sha, commit_count = github.get_commit_info(branch)
        version_tag = None

        if args.tags or args.releases:
            version = github.get_latest_version(use_tags=args.tags)
            version_tag = version.removeprefix(args.strip) if version and args.strip else version

        output = str(VersionInfo(commit_sha, commit_count, version_tag if not args.commits else None))

        # Apply regex if provided
        if args.regex:
            match = re.search(args.regex, output)
            if match:
                output = match.group(0)
            else:
                output = "No match found."

        print(output)

    except GitHubError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
