package audit

import "testing"

func TestV1RowDecodesActorFromLegacyField(t *testing.T) {
	row := []byte(`{"version":1,"record":{"id":"a-1","actor_name":"dan","action":"gdpr.erasure","resource":"c-1","tenant":"crestwood"}}`)
	r, err := DecodeStored(row)
	if err != nil { t.Fatal(err) }
	if r.Actor != "dan" || r.ID != "a-1" { t.Errorf("v1 decode=%+v", r) }
}

func TestV1RowCarriesIPWhenPresent(t *testing.T) {
	row := []byte(`{"version":1,"record":{"id":"a-2","actor_name":"priya","ip_address":"203.0.113.7","tenant":"vantage"}}`)
	r, _ := DecodeStored(row)
	if r.IPAddress != "203.0.113.7" { t.Errorf("ip=%q", r.IPAddress) }
}

func TestV2RowUnaffected(t *testing.T) {
	row := []byte(`{"version":2,"record":{"id":"a-3","actor":"sofia","tenant":"harborline"}}`)
	r, err := DecodeStored(row)
	if err != nil { t.Fatal(err) }
	if r.Actor != "sofia" { t.Errorf("v2 decode=%+v", r) }
}

func TestUnknownVersionIsRejected(t *testing.T) {
	if _, err := DecodeStored([]byte(`{"version":9,"record":{}}`)); err == nil {
		t.Fatal("unsupported envelope version must error")
	}
}
