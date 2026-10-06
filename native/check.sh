#!/bin/bash
set -euo pipefail
native_dir="$(cd "$(dirname "$0")" && pwd)"
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Library/Developer/CommandLineTools}"
export SDKROOT="${SDKROOT:-$DEVELOPER_DIR/SDKs/MacOSX26.5.sdk}"
export CLANG_MODULE_CACHE_PATH="${TMPDIR:-/private/tmp}/orbit-native-clang"
test_support="/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer"
args=(--build-system native --disable-sandbox --cache-path "${TMPDIR:-/private/tmp}/orbit-native-spm" --package-path "$native_dir" --scratch-path "$native_dir/.build-native")
if [[ -d "$test_support/usr/lib/XCTest.swiftmodule" && "$DEVELOPER_DIR" == "/Library/Developer/CommandLineTools" ]]; then
  # CLT builds the XCTest bundle but lacks its test discovery runner. Use the installed
  # Xcode test libraries directly; no Xcode GUI or licence acceptance is required.
  swift test "${args[@]}" --disable-swift-testing \
    -Xswiftc -I -Xswiftc "$test_support/usr/lib" \
    -Xswiftc -F -Xswiftc "$test_support/Library/Frameworks" \
    -Xlinker -rpath -Xlinker "$test_support/Library/Frameworks" \
    -Xlinker -rpath -Xlinker "$test_support/usr/lib" \
    -Xlinker "-L$test_support/usr/lib"
  binary_dir="$(swift build "${args[@]}" --show-bin-path)"
  "$test_support/Library/Xcode/Agents/xctest" "$binary_dir/OrbitMacPackageTests.xctest"
else
  swift test "${args[@]}"
fi
