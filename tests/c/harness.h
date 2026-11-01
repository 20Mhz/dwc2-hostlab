/* SPDX-License-Identifier: MIT */
/* Minimal test harness: no dependencies, runs anywhere the library builds. */
#ifndef HOSTLAB_TEST_HARNESS_H
#define HOSTLAB_TEST_HARNESS_H

#include <stdio.h>
#include <string.h>

extern int hl_test_failures;
extern int hl_test_checks;

#define CHECK(cond) do { \
	hl_test_checks++; \
	if (!(cond)) { \
		hl_test_failures++; \
		fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
	} \
} while (0)

#define CHECK_EQ(a, b) do { \
	long long _a = (long long)(a), _b = (long long)(b); \
	hl_test_checks++; \
	if (_a != _b) { \
		hl_test_failures++; \
		fprintf(stderr, "  FAIL %s:%d: %s == %s (%lld != %lld)\n", \
			__FILE__, __LINE__, #a, #b, _a, _b); \
	} \
} while (0)

#define CHECK_STR(a, b) do { \
	const char *_a = (a), *_b = (b); \
	hl_test_checks++; \
	if (strcmp(_a, _b)) { \
		hl_test_failures++; \
		fprintf(stderr, "  FAIL %s:%d: %s == \"%s\" (got \"%s\")\n", \
			__FILE__, __LINE__, #a, _b, _a); \
	} \
} while (0)

typedef void (*hl_test_fn)(void);

struct hl_test {
	const char *name;
	hl_test_fn fn;
};

#endif /* HOSTLAB_TEST_HARNESS_H */
