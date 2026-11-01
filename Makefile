# dwc2-hostlab build.
#
#   make                 native build (library, tools, tests) into build/host
#   make test            run C and Python unit tests natively
#   make armv6           static ARMv6 binaries for the Pi Zero W (Docker),
#                        tested under qemu-arm-static -cpu arm1176
#
# Cross builds reuse this Makefile with CC/BUILD/LDFLAGS/TEST_RUNNER overridden
# by scripts/build-armv6.sh.

BUILD       ?= build/host
CC          ?= cc
CFLAGS      ?= -O2 -g
# Required flags live apart from CFLAGS so a command-line CFLAGS cannot drop them.
HL_CFLAGS   := -std=c11 -D_GNU_SOURCE -Wall -Wextra -Werror -Wshadow \
               -Wstrict-prototypes -Wmissing-prototypes -Iinclude -Isrc/lib
LDFLAGS     ?=
TEST_RUNNER ?=
PYTHON      ?= python3

LIB_SRCS  := src/lib/usb_backend.c src/lib/backend_mock.c src/lib/backend_linux.c \
             src/lib/usbmon.c src/lib/jsonl.c src/lib/clock.c src/lib/policy.c
LIB_OBJS  := $(LIB_SRCS:%.c=$(BUILD)/%.o)
LIB       := $(BUILD)/libhostlab.a
TOOLS     := $(BUILD)/hostlab-ctl $(BUILD)/hostlab-mon
TESTS     := $(BUILD)/test_lib

.PHONY: all test test-c test-py armv6 clean
all: $(LIB) $(TOOLS) $(TESTS)

$(BUILD)/%.o: %.c $(wildcard include/hostlab/*.h src/lib/*.h tests/c/*.h)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $(HL_CFLAGS) -c -o $@ $<

$(LIB): $(LIB_OBJS)
	$(AR) rcs $@ $^

$(BUILD)/hostlab-%: $(BUILD)/src/tools/hostlab-%.o $(LIB)
	$(CC) $(CFLAGS) $(HL_CFLAGS) -o $@ $^ $(LDFLAGS)

$(BUILD)/test_lib: $(BUILD)/tests/c/test_lib.o $(LIB)
	$(CC) $(CFLAGS) $(HL_CFLAGS) -o $@ $^ $(LDFLAGS)

test: test-c test-py

test-c: $(TESTS) $(TOOLS)
	$(TEST_RUNNER) $(BUILD)/test_lib
	BUILD=$(BUILD) TEST_RUNNER="$(TEST_RUNNER)" sh tests/c/test_tools.sh

test-py:
	$(PYTHON) -m unittest discover -s python/tests -t python

armv6:
	scripts/build-armv6.sh

clean:
	rm -rf build
