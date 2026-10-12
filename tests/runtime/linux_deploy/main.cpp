#include <QCoreApplication>
#include <dlfcn.h>
#include <filesystem>
#include <iostream>

extern "C" int qcopilots_search_owned_fixture(void);

int main(int argc, char** argv)
{
    QCoreApplication application(argc, argv);
    if (argc != 2) return 1;
    if (qcopilots_search_owned_fixture() != 42) return 7;
    void* module = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!module) {
        std::cerr << dlerror() << '\n';
        return 2;
    }
    const auto operation = reinterpret_cast<int (*)()>(dlsym(module, "qcopilots_search_module_fixture"));
    if (!operation || operation() != 42) return 3;
    for (const auto* symbol : {"qcopilots_search_module_fixture", "qcopilots_search_owned_fixture",
                               "qcopilots_search_transitive_fixture"}) {
        Dl_info info{};
        void* address = dlsym(module, symbol);
        if (!address || !dladdr(address, &info) || !info.dli_fname) return 4;
        std::cout << std::filesystem::canonical(info.dli_fname).string() << '\n';
    }
    Dl_info qt{};
    if (!dladdr(reinterpret_cast<void*>(&qVersion), &qt) || !qt.dli_fname) return 5;
    std::cout << std::filesystem::canonical(qt.dli_fname).string() << '\n';
    return dlclose(module) == 0 ? 0 : 6;
}
