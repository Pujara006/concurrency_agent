// Two accounts; money moves between them while an auditor checks the total.
#include <iostream>
#include <thread>

int checking = 1000;
int savings = 1000;

void to_savings(int n) {
    for (int i = 0; i < n; i++) {
        checking -= 1;
        savings += 1;
    }
}

void to_checking(int n) {
    for (int i = 0; i < n; i++) {
        savings -= 1;
        checking += 1;
    }
}

int bad_audits = 0;   // only the auditor thread touches this

void auditor(int n) {
    for (int i = 0; i < n; i++) {
        int total = checking + savings;
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
