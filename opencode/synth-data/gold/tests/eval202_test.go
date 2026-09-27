package store

import (
	"errors"
	"testing"
)

func TestBulkDeleteWithoutNoteIsRejected(t *testing.T) {
	if err := NewBulkDeleter("").SoftDelete([]string{"po-1", "po-2"}); !errors.Is(err, ErrAuditNoteRequired) {
		t.Fatalf("bulk delete without audit note: err=%v", err)
	}
}

func TestSingleDeleteKeepsLegacyPath(t *testing.T) {
	if err := NewBulkDeleter("").SoftDelete([]string{"po-1"}); err != nil {
		t.Errorf("single-row helper must remain note-free: %v", err)
	}
}

func TestBulkDeleteWithNoteSucceeds(t *testing.T) {
	if err := NewBulkDeleter("quarterly cleanup, ticket OPS-91").SoftDelete([]string{"po-1", "po-2"}); err != nil {
		t.Fatal(err)
	}
}
