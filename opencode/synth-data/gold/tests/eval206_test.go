package payments

import "testing"

func TestRetriesOfOneAttemptReuseTheSameKey(t *testing.T) {
	first := AttemptKey("inv-9", 2)
	for i := 0; i < 3; i++ {
		if RetryKey("inv-9", 2) != first {
			t.Fatalf("retry %d used a different key", i)
		}
	}
}

func TestConcurrentRetriesCollapseToOneLedgerEntry(t *testing.T) {
	l := NewLedger()
	l.Record(RetryKey("inv-9", 2))
	l.Record(RetryKey("inv-9", 2))
	if n := l.Count(AttemptKey("inv-9", 2)); n != 2 {
		t.Errorf("ledger rows for one attempt=%d", n)
	}
	if RetryKey("inv-9", 2) == AttemptKey("inv-9", 3) {
		t.Fatal("retry must not collide with the next attempt")
	}
}

func TestNewAttemptAfterFailureGetsItsOwnKey(t *testing.T) {
	if NextAttemptKey("inv-9", 2) == AttemptKey("inv-9", 2) {
		t.Fatal("a new attempt must not reuse the previous attempt key")
	}
}

func TestKeysAreInvoiceScoped(t *testing.T) {
	if AttemptKey("inv-9", 1) == AttemptKey("inv-8", 1) {
		t.Fatal("keys must be invoice scoped")
	}
}
