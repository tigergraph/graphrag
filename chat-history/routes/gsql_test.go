package routes

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func serve(t *testing.T, routes map[string]string) (string, string) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if body, ok := routes[r.URL.Path]; ok {
			w.Write([]byte(body))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(srv.Close)
	i := strings.LastIndex(srv.URL, ":")
	return srv.URL[:i], srv.URL[i+1:]
}

const showUser = "  - Name: admin\n    - Global Roles: superuser, globaldesigner\n"

func TestExecuteGSQLOnTigerGraph4(t *testing.T) {
	host, port := serve(t, map[string]string{"/gsql/v1/statements": showUser})
	out, err := executeGSQL(host, "admin", "pw", "SHOW USER", port)
	if err != nil {
		t.Fatal(err)
	}
	roles := parseUserRoles(out, "admin")
	if !hasAdminAccess(roles, []string{"superuser"}) {
		t.Fatalf("roles %v", roles)
	}
}

func TestExecuteGSQLOnTigerGraph3(t *testing.T) {
	host, port := serve(t, map[string]string{"/gsqlserver/gsql/file": showUser})
	out, err := executeGSQL(host, "admin", "pw", "SHOW USER", port)
	if err != nil || !strings.Contains(out, "Global Roles") {
		t.Fatalf("out=%q err=%v", out, err)
	}
}

func TestExecuteGSQLReportsFailure(t *testing.T) {
	host, port := serve(t, map[string]string{})
	if _, err := executeGSQL(host, "admin", "pw", "SHOW USER", port); err == nil {
		t.Fatal("expected an error when no GSQL endpoint answers")
	}
}
