#!/bin/sh
# SPDX-License-Identifier: MIT
# End-to-end checks of the CLI tools against the mock backend.
set -eu

: "${BUILD:=build/host}"
: "${TEST_RUNNER:=}"
CTL="$TEST_RUNNER $BUILD/hostlab-ctl --backend mock"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0

check() { # description, command...
    desc=$1; shift
    if "$@"; then echo "tools: $desc ok"; else echo "tools: $desc FAILED"; fail=1; fi
}

$CTL list > "$tmp/list.jsonl"
check "list reports the default mock device" grep -q '"vid":4617,"pid":1' "$tmp/list.jsonl"

$CTL get-descriptor 1:2 > "$tmp/gd.jsonl"
check "get-descriptor returns 18 bytes" grep -q '"status":"ok","errno":0,"actual":18,"data":"120100' "$tmp/gd.jsonl"
check "output starts with meta" sh -c "head -1 '$tmp/gd.jsonl' | grep -q '\"ev\":\"meta\"'"
check "output ends with clock snapshot" sh -c "tail -1 '$tmp/gd.jsonl' | grep -q '\"ev\":\"clock\"'"

if $CTL control 1:2 --setup 00,09,0001,0000,0000 > "$tmp/w.jsonl" 2>/dev/null; then
    echo "tools: write without --allow-write was accepted FAILED"; fail=1
else
    check "write without --allow-write is refused" grep -q '"op":"policy"' "$tmp/w.jsonl"
fi

$CTL cancel-sweep 1:2 --delays-us 0:1500:500 > "$tmp/cs.jsonl"
check "sweep cancels before completion (0 us)" grep -q '"delay_us":0,.*"cancel":"ok","status":"cancelled"' "$tmp/cs.jsonl"
check "sweep is too late after completion (1500 us)" grep -q '"delay_us":1500,.*"cancel":"not_pending","status":"ok"' "$tmp/cs.jsonl"
check "sweep writes summary" grep -q '"ev":"summary"' "$tmp/cs.jsonl"

if $CTL reset 1:2 > /dev/null 2>&1; then
    echo "tools: reset without --allow-reset was accepted FAILED"; fail=1
else
    echo "tools: reset without --allow-reset is refused ok"
fi

exit $fail
