extern int qcopilots_search_transitive_fixture(void);
int qcopilots_search_owned_fixture(void) { return qcopilots_search_transitive_fixture() + 1; }
