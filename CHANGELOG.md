# Changelog

## [v0.0.8] - 2026-05-14

- Adding back in DRONE_APP_BASE_URL as it is required for github link out.

## [v0.0.7] - 2026-05-14

- One last update for latest release to work properly.

## [v0.0.6] - 2026-05-14

- updating create release script to fix "latest" bug
- Cleaning up curl request to run the app on batocera machines.

## [v0.0.5] - 2026-05-14

- Updating create-release.sh
- Enhance create-release.sh with latest tag and changelog updates
- Context: There are two apps: - batocera.drone: runs on each Batocera device - batocera.overmind: central fleet management app - Overlord = user - Swarm = group of drones under an overlord - Drone device_id should be the MAC address - Demo user: demo@example.com
- Updating project image.
- Add logo image to README
- Uploading Hive Mind image
- Adding more overmind integration code for different action processing.
- Updating README to not be as technical.  Adding more technical pieces to bottom under Advanced User.
- for Drone: Update the scraper import to also pull any metadata info as well as media if available.  It looks like scraping mobygames might not be possible due to captcha / cloudflare.  We can remove the scraping for mobygames but let's leave the link out so people can navigate to the site manually.  admin artwork rom matches panel contains launchbox but isn’t defaulting <name> search like it should.  remove <system> from TheGamesDB link out.  Remove <system> from mobygames link out.

All notable changes to Batocera Drone will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v0.0.4] - 2026-05-12

## [v0.0.3] - 2026-05-12
## [v0.0.2] - 2026-05-12
## [v0.0.1] - 2026-05-12
## [v0.0.1] - 2026-05-12

### Added
- Initial release pipeline with GitHub Actions
- Automated release notes generation
- Release script for local creation
