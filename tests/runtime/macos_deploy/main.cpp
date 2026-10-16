#include <QCoreApplication>
#include <dlfcn.h>
#include <filesystem>
#include <iostream>

extern "C" int qcopilots_search_owned_fixture(void);

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    if (qcopilots_search_owned_fixture() != 42) return 1;
    for (const char *symbol : {"qcopilots_search_owned_fixture", "qcopilots_search_transitive_fixture"}) {
        Dl_info info{};
        const auto address = dlsym(RTLD_DEFAULT, symbol);
        if (!address || !dladdr(address, &info) || !info.dli_fname) return 2;
        std::cout << std::filesystem::canonical(info.dli_fname).string() << '\n';
    }
    Dl_info qt{};
    if (!dladdr(reinterpret_cast<const void *>(&qVersion), &qt) || !qt.dli_fname) return 3;
    std::cout << std::filesystem::canonical(qt.dli_fname).string() << '\n';
    return 0;
}
