#include <iostream>
#include <mutex>
#include <thread>

std::mutex m1;
std::mutex m2;

void func1(){
    for (int i = 0; i < 1000;i++){
        std::scoped_lock lk(m1, m2);
    }
}

void func2(){
    for (int i = 0; i < 1000;i++){
        std::scoped_lock lk(m1, m2);
    }
}

int main(){
    std::thread t1(func1);
    std::thread t2(func2);
    t1.join();
    t2.join();
    std::cout << "done" << std::endl;
    return 0;
}
