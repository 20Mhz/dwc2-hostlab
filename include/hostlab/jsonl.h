/* SPDX-License-Identifier: MIT */
/* Minimal JSON Lines writer: one flat object per line. */
#ifndef HOSTLAB_JSONL_H
#define HOSTLAB_JSONL_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Trace schema version written in every "meta" record. Bump on any
 * incompatible change to field names or meanings (see docs/trace-format.md). */
#define HL_TRACE_SCHEMA 1

struct hl_jsonl {
	FILE *f;
	int nfields;
};

void hl_jsonl_init(struct hl_jsonl *j, FILE *f);
void hl_jsonl_begin(struct hl_jsonl *j);
void hl_jsonl_end(struct hl_jsonl *j); /* writes "}\n" and flushes */
void hl_jsonl_str(struct hl_jsonl *j, const char *key, const char *val);
void hl_jsonl_u64(struct hl_jsonl *j, const char *key, uint64_t val);
void hl_jsonl_i64(struct hl_jsonl *j, const char *key, int64_t val);
void hl_jsonl_bool(struct hl_jsonl *j, const char *key, int val);
void hl_jsonl_hex(struct hl_jsonl *j, const char *key, const uint8_t *buf, size_t len);
void hl_jsonl_null(struct hl_jsonl *j, const char *key);

#ifdef __cplusplus
}
#endif

#endif /* HOSTLAB_JSONL_H */
