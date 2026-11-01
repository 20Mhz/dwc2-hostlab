// SPDX-License-Identifier: MIT
#include <inttypes.h>

#include "hostlab/jsonl.h"

void hl_jsonl_init(struct hl_jsonl *j, FILE *f)
{
	j->f = f;
	j->nfields = 0;
}

void hl_jsonl_begin(struct hl_jsonl *j)
{
	fputc('{', j->f);
	j->nfields = 0;
}

void hl_jsonl_end(struct hl_jsonl *j)
{
	fputs("}\n", j->f);
	fflush(j->f);
}

static void put_string(FILE *f, const char *s)
{
	fputc('"', f);
	for (; *s; s++) {
		unsigned char c = (unsigned char)*s;

		if (c == '"' || c == '\\')
			fprintf(f, "\\%c", c);
		else if (c < 0x20)
			fprintf(f, "\\u%04x", c);
		else
			fputc(c, f);
	}
	fputc('"', f);
}

static void key(struct hl_jsonl *j, const char *k)
{
	if (j->nfields++)
		fputc(',', j->f);
	put_string(j->f, k);
	fputc(':', j->f);
}

void hl_jsonl_str(struct hl_jsonl *j, const char *k, const char *val)
{
	key(j, k);
	put_string(j->f, val ? val : "");
}

void hl_jsonl_u64(struct hl_jsonl *j, const char *k, uint64_t val)
{
	key(j, k);
	fprintf(j->f, "%" PRIu64, val);
}

void hl_jsonl_i64(struct hl_jsonl *j, const char *k, int64_t val)
{
	key(j, k);
	fprintf(j->f, "%" PRId64, val);
}

void hl_jsonl_bool(struct hl_jsonl *j, const char *k, int val)
{
	key(j, k);
	fputs(val ? "true" : "false", j->f);
}

void hl_jsonl_hex(struct hl_jsonl *j, const char *k, const uint8_t *buf, size_t len)
{
	key(j, k);
	fputc('"', j->f);
	for (size_t i = 0; i < len; i++)
		fprintf(j->f, "%02x", buf[i]);
	fputc('"', j->f);
}

void hl_jsonl_null(struct hl_jsonl *j, const char *k)
{
	key(j, k);
	fputs("null", j->f);
}
