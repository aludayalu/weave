package payroll

import (
	"os"
	"reflect"
	"strings"
	"testing"
)

func TestReservedComponentPositionsUnchanged(t *testing.T) {
	// MER-201: finance's workbook binds positions 4-11. The new shift
	// differential must be APPENDED, not inserted.
	want := []string{"BASE", "OT", "BONUS", "COMM", "HEALTH", "RETIRE", "MEAL", "UNIFORM"}
	for i, code := range want {
		if PAY_COMPONENT_ORDER[i] != code {
			t.Fatalf("position %d is %q, finance template binds %q", i, PAY_COMPONENT_ORDER[i], code)
		}
	}
}

func TestNewComponentIsAppendedAndSchemaBumped(t *testing.T) {
	if got := PAY_COMPONENT_ORDER[len(PAY_COMPONENT_ORDER)-1]; got != "SHIFT_DIFF" {
		t.Errorf("new component must append, tail is %q", got)
	}
	if SchemaVersion < 5 {
		t.Errorf("schema version = %d; workbook importer needs 5", SchemaVersion)
	}
}

func TestFrontendOrderMirrorsGoRegistry(t *testing.T) {
	src, err := os.ReadFile("../../web/lib/payComponents.ts")
	if err != nil { t.Fatal(err) }
	for i, code := range ComponentOrder() {
		needle := `code: "` + code + `"`
		if !strings.Contains(string(src), needle) {
			t.Errorf("frontend mirror missing %q at position %d", code, i)
		}
	}
	if !reflect.DeepEqual(ComponentOrder(), PAY_COMPONENT_ORDER) {
		t.Error("ComponentOrder must return the live registry")
	}
}
