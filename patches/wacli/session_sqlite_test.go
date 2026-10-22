package wa

import (
	"bytes"
	"context"
	"database/sql"
	"runtime"
	"testing"
	"time"

	"github.com/openclaw/wacli/internal/sqliteutil"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

func TestBackupSessionReadsDuringWrite(t *testing.T) {
	// Start with a legacy session so the test also covers existing logins.
	path := newPairedSessionStore(t)
	c, err := New(Options{StorePath: path})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	device := c.client.Store
	keyID := []byte{1}
	key := store.AppStateSyncKey{Data: bytes.Repeat([]byte{1}, 32), Fingerprint: []byte{1}, Timestamp: 1}
	if err := device.AppStateKeys.PutAppStateSyncKey(t.Context(), keyID, key); err != nil {
		t.Fatal(err)
	}
	contact := types.NewJID("15550000001", types.DefaultUserServer)
	if err := device.Contacts.PutContactName(t.Context(), contact, "Test", "Test Contact"); err != nil {
		t.Fatal(err)
	}

	// Hold an uncommitted write while the real key and contact stores read.
	db, err := sql.Open("sqlite3", sqliteutil.FileURI(path, "_txlock=exclusive"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	tx, err := db.BeginTx(t.Context(), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("UPDATE whatsmeow_contacts SET full_name = 'Uncommitted'"); err != nil {
		t.Fatal(err)
	}

	t.Run("app state key", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(t.Context(), time.Second)
		defer cancel()
		got, err := device.AppStateKeys.GetAppStateSyncKey(ctx, keyID)
		if err != nil {
			t.Fatalf("read app state key during write: %v", err)
		}
		if got == nil || !bytes.Equal(got.Data, key.Data) {
			t.Fatalf("stored key changed: %v", got)
		}
	})
	t.Run("contacts", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(t.Context(), time.Second)
		defer cancel()
		contacts, err := device.Contacts.GetAllContacts(ctx)
		if err != nil {
			t.Fatalf("read contacts during write: %v", err)
		}
		if got := contacts[contact].FullName; got != "Test Contact" {
			t.Fatalf("contact name = %q, want committed name", got)
		}
	})
}

func TestBackupSessionConcurrentTransactions(t *testing.T) {
	c, err := New(Options{StorePath: newPairedSessionStore(t)})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	device := c.client.Store
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	const workers = 8
	const collection = string(appstate.WAPatchRegular)
	start := make(chan struct{})
	results := make(chan error, workers)
	for range workers {
		go func() {
			<-start
			results <- device.EventBuffer.DoDecryptionTxn(ctx, func(ctx context.Context) error {
				version, hash, err := device.AppState.GetAppStateVersion(ctx, collection)
				if err != nil {
					return err
				}
				// Let competing transactions try to read before this write.
				runtime.Gosched()
				return device.AppState.PutAppStateVersion(ctx, collection, version+1, hash)
			})
		}()
	}
	close(start)
	for range workers {
		if err := <-results; err != nil {
			t.Errorf("concurrent session transaction: %v", err)
		}
	}
	version, _, err := device.AppState.GetAppStateVersion(ctx, collection)
	if err != nil {
		t.Fatal(err)
	}
	if version != workers {
		t.Fatalf("version = %d, want %d committed updates", version, workers)
	}
}
