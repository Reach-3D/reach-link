BINARY_NAME := reach-link
TARGET_DIR  := target/release
BUILD_DIR   := build/artifacts

.PHONY: build test clean

## build: compile a release binary for the host architecture
build:
	cargo build --release
	@echo "Binary: $(TARGET_DIR)/$(BINARY_NAME)"

## test: run the test suite
test:
	cargo test

## clean: remove build artifacts
clean:
	cargo clean
	rm -rf $(BUILD_DIR)

## cross: cross-compile for arm64 and x86_64 (requires `cross`)
cross:
	bash build/cross-build.sh

## help: list available targets
help:
	@grep -E '^##' Makefile | sed 's/## //'
