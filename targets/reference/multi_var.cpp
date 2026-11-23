// Two accounts; money moves between them while an auditor checks the total.
#include <barrier>
#include <iostream>
#include <mutex>
#include <thread>

std::mutex accounts_mutex;   // guards checking and savings together
int checking = 1000;
int savings = 1000;
std::barrier phase_sync(3);   // all three threads finish phase 1 before starting phase 2

void to_savings(int n) {
    for (int i = 0; i < n / 2; i++) {
        std::lock_guard<std::mutex> lock(accounts_mutex);
        checking -= 1;
        savings += 1;
    }
    phase_sync.arrive_and_wait();
    for (int i = 0; i < n / 2; i++) {
        std::lock_guard<std::mutex> lock(accounts_mutex);
        checking -= 1;
        savings += 1;
    }
}

void to_checking(int n) {
    for (int i = 0; i < n / 2; i++) {
        std::lock_guard<std::mutex> lock(accounts_mutex);
        savings -= 1;
        checking += 1;
    }
    phase_sync.arrive_and_wait();
    for (int i = 0; i < n / 2; i++) {
        std::lock_guard<std::mutex> lock(accounts_mutex);
        savings -= 1;
        checking += 1;
    }
}

int bad_audits = 0;   // only the auditor thread touches this

void auditor(int n) {
    for (int i = 0; i < n / 2; i++) {
        int total;
        {
            std::lock_guard<std::mutex> lock(accounts_mutex);
            total = checking + savings;
        }
        if (total != 2000) bad_audits++;
    }
    phase_sync.arrive_and_wait();
    for (int i = 0; i < n / 2; i++) {
        int total;
        {
            std::lock_guard<std::mutex> lock(accounts_mutex);
            total = checking + savings;
        }
        if (total != 2000) bad_audits++;
    }
}

int main() {
    std::thread a(to_savings, 100000);
    std::thread b(to_checking, 100000);
    std::thread c(auditor, 100000);
    a.join();
    b.join();
    c.join();
    std::cout << "checking=" << checking << " savings=" << savings
              << " total=" << checking + savings << std::endl;
    if (bad_audits > 0) {
        std::cout << "invariant violated in " << bad_audits << " audits" << std::endl;
        return 1;
    }
    return 0;
}
