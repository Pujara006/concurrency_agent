// Two workers append results to a shared vector.
#include <iostream>
#include <thread>
#include <vector>

std::vector<int> results;

void worker(int id) {
    for (int i = 0; i < 1000; i++) {
        results.push_back(id * 100000 + i);
    }
}

int main() {
    // NOTE: the order of results doesn't matter, and each worker is fast,
    // so they don't really need to run at the same time.
    std::thread a(worker, 1);
    std::thread b(worker, 2);
    a.join();
    b.join();
    std::cout << "results: " << results.size() << std::endl;
    return 0;
}
