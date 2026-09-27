package billing

import (
	"testing"
	"time"
)

func mustLoad(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation(name)
	if err != nil { t.Skipf("tzdata unavailable: %v", err) }
	return loc
}

func TestOverdueUsesTenantLocalDate(t *testing.T) {
	denver := mustLoad(t, "America/Denver")
	// 2026-02-03T02:00Z is still 2026-02-02 in Denver (UTC-7).
	clock := TenantClock{Location: denver, Now: time.Date(2026, 2, 3, 2, 0, 0, 0, time.UTC)}
	due := time.Date(2026, 2, 2, 23, 59, 0, 0, denver)
	if clock.IsOverdue(due) {
		t.Error("invoice not yet past due in tenant-local terms was marked overdue")
	}
}

func TestOverdueFlipsAtTenantLocalMidnight(t *testing.T) {
	denver := mustLoad(t, "America/Denver")
	clock := TenantClock{Location: denver, Now: time.Date(2026, 2, 3, 8, 0, 0, 0, time.UTC)}
	due := time.Date(2026, 2, 2, 23, 59, 0, 0, denver)
	if !clock.IsOverdue(due) {
		t.Error("invoice past due in tenant-local terms must be overdue")
	}
}

func TestLocalDateFormat(t *testing.T) {
	ny := mustLoad(t, "America/New_York")
	clock := TenantClock{Location: ny, Now: time.Date(2026, 5, 4, 3, 0, 0, 0, time.UTC)}
	if got := clock.LocalDate(); got != "2026-05-03" {
		t.Errorf("local date=%q want 2026-05-03", got)
	}
}
